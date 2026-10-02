"""Patient identity encrypted in a real database, as the application role. Synthetic data only."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.crypto.blind_index import BlindIndexer
from app.crypto.errors import DecryptionError, KeyUnavailableError
from app.crypto.keyring import FieldSealer, KeyRing, KeyWrapper
from app.crypto.keystore import KeyStore, destroy_patient_key
from app.timeline.ingest import SealContext
from app.timeline.models import DataKey, PatientBlindIndex
from app.timeline.patient_identity import (
    PatientIdentifier,
    PatientIdentity,
    create_patient,
    find_patients,
    read_identity,
    replace_identity,
)
from tests.conftest import APP_ROLE

pytestmark = pytest.mark.db

SENTINEL_FAMILY_NAME = "Quillfeather-Sentinel"
MRN = PatientIdentifier("http://example.test/mrn", "MRN-0001")
SSN_SHAPED = PatientIdentifier("http://hl7.org/fhir/sid/us-ssn", "999-00-0001")

ABE = PatientIdentity(
    given_names=("Abe604",),
    family_name=SENTINEL_FAMILY_NAME,
    birth_date=date(1970, 1, 1),
    identifiers=(MRN, SSN_SHAPED),
    sex_at_birth="male",
)
BEA = PatientIdentity(
    given_names=("Bea",),
    family_name="Ostrander",
    birth_date=date(1982, 5, 17),
    identifiers=(PatientIdentifier("http://example.test/mrn", "MRN-0002"),),
    sex_at_birth="female",
)
CAL = PatientIdentity(
    given_names=("Cal",),
    family_name=SENTINEL_FAMILY_NAME,
    birth_date=date(1955, 11, 3),
)


@dataclass
class Crypto:
    """One run's key handling: what a fresh process builds from the same secrets."""

    wrapper: KeyWrapper
    ring: KeyRing
    keystore: KeyStore
    sealer: FieldSealer
    indexer: BlindIndexer


def _crypto(kek: bytes, blind_key: bytes, kek_version: int = 1) -> Crypto:
    wrapper = KeyWrapper(kek, kek_version)
    ring = KeyRing()
    return Crypto(
        wrapper, ring, KeyStore(wrapper, ring), FieldSealer(ring), BlindIndexer(blind_key)
    )


@pytest.fixture
def secrets() -> tuple[bytes, bytes]:
    return os.urandom(32), os.urandom(32)


@pytest.fixture
def crypto(secrets: tuple[bytes, bytes]) -> Crypto:
    return _crypto(*secrets)


@asynccontextmanager
async def _session(engine: AsyncEngine, *, as_app: bool = True) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine) as session, session.begin():
        if as_app:
            await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        yield session


class _Abandon(Exception):  # noqa: N818  # a signal to roll back, not an error
    pass


async def _load_then_roll_back(
    engine: AsyncEngine, keystore: KeyStore, owners: list[uuid.UUID | None]
) -> None:
    """Load keys in a transaction and abandon it, so the transaction rolls back."""
    async with _session(engine) as session:
        await keystore.load(session, owners)
        raise _Abandon


async def _create(engine: AsyncEngine, crypto: Crypto, identity: PatientIdentity) -> uuid.UUID:
    async with _session(engine) as session:
        return await create_patient(
            session, identity, crypto.keystore, crypto.sealer, crypto.indexer
        )


async def _as_owner(engine: AsyncEngine, statement: str, **parameters: object) -> None:
    async with _session(engine, as_app=False) as session:
        await session.execute(text(statement), parameters)


async def _enc_columns(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND column_name LIKE '%\\_enc' ORDER BY 1, 2"
            )
        )
        return [(row.table_name, row.column_name) for row in rows]


async def _plaintext_found_in_enc_columns(
    engine: AsyncEngine, needles: Iterable[str]
) -> list[tuple[str, str, str]]:
    """Every (table, column, needle) where an encrypted column holds the needle as plain bytes."""
    found = []
    columns = await _enc_columns(engine)
    async with engine.connect() as connection:
        for table, column in columns:
            for needle in needles:
                count = await connection.scalar(
                    text(
                        f'SELECT count(*) FROM "{table}"'  # noqa: S608  # names come from the catalog
                        f' WHERE position(CAST(:needle AS bytea) IN "{column}") > 0'
                    ),
                    {"needle": needle.encode()},
                )
                if count:
                    found.append((table, column, needle))
    return found


async def test_an_identity_round_trips_through_the_database(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    patient_id = await _create(engine, crypto, ABE)

    async with _session(engine) as session:
        assert await read_identity(session, patient_id, crypto.sealer) == ABE


@pytest.mark.parametrize(
    "given_names",
    [
        pytest.param(("Mary Ann",), id="one given name with a space"),
        pytest.param(("Mary Ann", "Lou"), id="several, one with a space"),
        pytest.param(("Zoë", "O'Neil"), id="accents and an apostrophe"),
        pytest.param((), id="none"),
    ],
)
async def test_given_names_round_trip_exactly_and_are_still_findable(
    engine: AsyncEngine, crypto: Crypto, given_names: tuple[str, ...]
) -> None:
    identity = PatientIdentity(given_names, "Ostrander", date(1990, 2, 3))
    patient_id = await _create(engine, crypto, identity)

    async with _session(engine) as session:
        assert await read_identity(session, patient_id, crypto.sealer) == identity
        if given_names:
            first_token = crypto.indexer.name_tokens([given_names[0]]).pop()
            assert first_token  # the name has letters, so it has an index entry
            assert await find_patients(session, crypto.indexer, name=given_names[0]) == [patient_id]


async def test_the_encrypted_columns_hold_ciphertext_and_no_plaintext(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    patient_id = await _create(engine, crypto, ABE)
    await _create(engine, crypto, BEA)

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT given_name_enc, family_name_enc, birth_date_enc, identifiers_enc"
                    " FROM patient WHERE id = :id"
                ),
                {"id": patient_id},
            )
        ).one()
    for sealed in row:
        assert bytes(sealed)[0] == 1
        assert len(bytes(sealed)) >= 29
    needles = [SENTINEL_FAMILY_NAME, "Abe604", "Ostrander", "1970-01-01", "MRN-0001", "999-00-0001"]
    assert {
        ("patient", "given_name_enc"),
        ("patient", "family_name_enc"),
        ("patient", "birth_date_enc"),
        ("patient", "identifiers_enc"),
        ("source_record", "payload_enc"),
        ("timeline_event", "value_text_enc"),
        ("timeline_event", "detail_enc"),
    } <= set(await _enc_columns(engine))
    assert await _plaintext_found_in_enc_columns(engine, needles) == []


async def test_a_plaintext_scan_can_find_plaintext_when_there_is_some(engine: AsyncEngine) -> None:
    # The scan is only worth trusting if it catches a deliberate leak.
    async with _session(engine, as_app=False) as session:
        await session.execute(
            text("INSERT INTO patient (id, family_name_enc) VALUES (gen_random_uuid(), :leak)"),
            {"leak": SENTINEL_FAMILY_NAME.encode()},
        )

    leaks = await _plaintext_found_in_enc_columns(engine, [SENTINEL_FAMILY_NAME])

    assert leaks == [("patient", "family_name_enc", SENTINEL_FAMILY_NAME)]


async def test_ciphertext_swapped_between_patients_or_columns_does_not_open(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    abe = await _create(engine, crypto, ABE)
    bea = await _create(engine, crypto, BEA)

    await _as_owner(
        engine,
        "UPDATE patient SET family_name_enc = (SELECT family_name_enc FROM patient WHERE id = :a)"
        " WHERE id = :b",
        a=abe,
        b=bea,
    )
    async with _session(engine) as session:
        with pytest.raises(DecryptionError):
            await read_identity(session, bea, crypto.sealer)

    await _as_owner(
        engine, "UPDATE patient SET given_name_enc = family_name_enc WHERE id = :a", a=abe
    )
    async with _session(engine) as session:
        with pytest.raises(DecryptionError):
            await read_identity(session, abe, crypto.sealer)


async def test_a_destroyed_patient_key_makes_that_patients_data_unreadable(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    writer = _crypto(*secrets)
    abe = await _create(engine, writer, ABE)
    bea = await _create(engine, writer, BEA)
    async with _session(engine) as session:
        assert await find_patients(session, writer.indexer, name="Abe") == [abe]

    async with _session(engine, as_app=False) as session:
        assert await destroy_patient_key(session, abe) is True
    async with _session(engine, as_app=False) as session:
        assert await destroy_patient_key(session, abe) is False

    async with _session(engine) as session:
        for criteria in (
            {"name": "Abe"},
            {"birth_date": ABE.birth_date},
            {"identifier": MRN},
        ):
            assert await find_patients(session, writer.indexer, **criteria) == []
        assert await find_patients(session, writer.indexer, name="Bea") == [bea]
        remaining = await session.scalar(
            select(func.count())
            .select_from(PatientBlindIndex)
            .where(PatientBlindIndex.patient_id == abe)
        )
    assert remaining == 0

    restarted = _crypto(*secrets)  # a new process: only the KEK, no keys in memory
    async with _session(engine) as session:
        with pytest.raises(KeyUnavailableError, match="destroyed"):
            await restarted.keystore.load(session, [abe])
        await restarted.keystore.load(session, [bea])
        assert await read_identity(session, bea, restarted.sealer) == BEA
        with pytest.raises(KeyUnavailableError):
            await read_identity(session, abe, restarted.sealer)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(DataKey.wrapped_key, DataKey.destroyed_at).where(DataKey.patient_id == abe)
            )
        ).one()
    assert row.wrapped_key is None
    assert row.destroyed_at is not None


async def test_a_key_created_in_a_rolled_back_transaction_is_not_kept_in_the_ring(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    run = _crypto(*secrets)
    context = SealContext("source_record", "payload_enc", uuid.uuid4(), None)

    with pytest.raises(_Abandon):
        await _load_then_roll_back(engine, run.keystore, [None])
    assert None not in run.ring

    async with _session(engine) as session:
        await run.keystore.load(session, [None])
    sealed = run.sealer.seal(b"practitioner", context)

    later = _crypto(*secrets)  # a new process reads the committed key, not the abandoned one
    async with _session(engine) as session:
        await later.keystore.load(session, [None])
    assert later.sealer.open(sealed, context) == b"practitioner"


async def test_a_committed_key_survives_a_later_rollback(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    async with _session(engine) as session:
        await crypto.keystore.load(session, [None])

    with pytest.raises(_Abandon):
        await _load_then_roll_back(engine, crypto.keystore, [None])

    assert None in crypto.ring


async def test_a_key_destroyed_by_another_process_stops_working_at_the_next_load(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    running = _crypto(*secrets)
    abe = await _create(engine, running, ABE)
    assert abe in running.ring
    async with _session(engine, as_app=False) as session:
        await destroy_patient_key(session, abe)

    async with _session(engine) as session:
        with pytest.raises(KeyUnavailableError, match="destroyed"):
            await running.keystore.load(session, [abe])
        with pytest.raises(KeyUnavailableError):
            await read_identity(session, abe, running.sealer)
    assert abe not in running.ring


async def test_loading_many_keys_costs_one_query_for_the_lookup(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    writer = _crypto(*secrets)
    ids = [await _create(engine, writer, identity) for identity in (ABE, BEA, CAL)]
    statements: list[str] = []

    def count(_c: object, _cur: object, statement: str, *_rest: object) -> None:
        if "FROM data_key" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        async with _session(engine) as session:
            await _crypto(*secrets).keystore.load(session, [*ids, None])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)

    assert len(statements) == 1


async def test_a_stored_key_of_the_wrong_length_is_refused_by_the_database(
    engine: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError, match="wrapped_key_has_wrapped_length"):
        await _as_owner(
            engine,
            "INSERT INTO data_key (kek_version, wrapped_key) VALUES (1, :short)",
            short=os.urandom(10),
        )


async def test_a_key_cannot_be_marked_destroyed_while_it_is_still_stored(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    patient_id = await _create(engine, crypto, ABE)

    with pytest.raises(IntegrityError, match="key_present_unless_destroyed"):
        await _as_owner(
            engine, "UPDATE data_key SET destroyed_at = now() WHERE patient_id = :id", id=patient_id
        )


async def test_a_new_process_with_the_same_secrets_reads_what_an_earlier_one_wrote(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    patient_id = await _create(engine, _crypto(*secrets), ABE)

    later = _crypto(*secrets)
    async with _session(engine) as session:
        await later.keystore.load(session, [patient_id])
        assert await read_identity(session, patient_id, later.sealer) == ABE


async def test_the_wrong_kek_or_kek_version_cannot_load_a_key(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    patient_id = await _create(engine, _crypto(*secrets), ABE)

    async with _session(engine) as session:
        with pytest.raises(KeyUnavailableError, match="cannot be unwrapped"):
            await _crypto(os.urandom(32), secrets[1]).keystore.load(session, [patient_id])
    async with _session(engine) as session:
        with pytest.raises(KeyUnavailableError, match="version 1"):
            await _crypto(secrets[0], secrets[1], kek_version=2).keystore.load(
                session, [patient_id]
            )


async def test_two_runs_creating_the_system_key_at_once_end_up_with_one_key(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    first, second = _crypto(*secrets), _crypto(*secrets)

    async def load(crypto: Crypto) -> None:
        async with _session(engine) as session:
            await crypto.keystore.load(session, [None])

    await asyncio.gather(load(first), load(second))

    assert first.ring.get(None) == second.ring.get(None)
    async with engine.connect() as connection:
        rows = await connection.scalar(
            select(func.count()).select_from(DataKey).where(DataKey.patient_id.is_(None))
        )
    assert rows == 1
    context = SealContext("source_record", "payload_enc", uuid.uuid4(), None)
    assert (
        second.sealer.open(first.sealer.seal(b"practitioner", context), context) == b"practitioner"
    )


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        pytest.param({"name": "Abe Quillfeather-Sentinel"}, ["abe"], id="full name"),
        pytest.param({"name": "abe"}, ["abe"], id="given name, any case"),
        pytest.param({"name": "QUILLFEATHER"}, ["abe", "cal"], id="shared family token"),
        pytest.param({"birth_date": date(1982, 5, 17)}, ["bea"], id="birth date"),
        pytest.param({"identifier": MRN}, ["abe"], id="identifier"),
        pytest.param({"name": "sentinel", "birth_date": date(1955, 11, 3)}, ["cal"], id="both"),
        pytest.param({"name": "Abe Ostrander"}, [], id="tokens of two patients"),
        pytest.param({"name": "Quillfeathers"}, [], id="near miss"),
        pytest.param({"birth_date": date(1970, 1, 2)}, [], id="wrong birth date"),
        pytest.param({"name": "42"}, [], id="a name with no letters"),
        pytest.param(
            {"identifier": PatientIdentifier(MRN.system, "MRN-9999")}, [], id="unknown identifier"
        ),
    ],
)
async def test_exact_match_lookup_finds_exactly_the_matching_patients(
    engine: AsyncEngine,
    crypto: Crypto,
    criteria: dict[str, object],
    expected: list[str],
) -> None:
    ids = {
        "abe": await _create(engine, crypto, ABE),
        "bea": await _create(engine, crypto, BEA),
        "cal": await _create(engine, crypto, CAL),
    }

    async with _session(engine) as session:
        found = await find_patients(session, crypto.indexer, **criteria)  # type: ignore[arg-type]

    assert found == sorted(ids[name] for name in expected)


async def test_a_lookup_with_nothing_to_look_up_is_refused(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    async with _session(engine) as session:
        with pytest.raises(ValueError, match="at least one"):
            await find_patients(session, crypto.indexer)


async def test_a_lookup_with_another_blind_index_key_finds_nothing(
    engine: AsyncEngine, secrets: tuple[bytes, bytes]
) -> None:
    await _create(engine, _crypto(*secrets), ABE)

    async with _session(engine) as session:
        found = await find_patients(session, BlindIndexer(os.urandom(32)), name="abe")

    assert found == []


async def test_replacing_an_identity_reseals_it_and_rebuilds_the_indexes(
    engine: AsyncEngine, crypto: Crypto
) -> None:
    patient_id = await _create(engine, crypto, ABE)
    renamed = PatientIdentity(
        given_names=("Mallory",),
        family_name="Ostrander",
        birth_date=date(1970, 1, 1),
        identifiers=(MRN,),
        sex_at_birth="female",
    )

    async with _session(engine) as session:
        await replace_identity(session, patient_id, renamed, crypto.sealer, crypto.indexer)
    async with _session(engine) as session:
        assert await read_identity(session, patient_id, crypto.sealer) == renamed
        assert await find_patients(session, crypto.indexer, name="Abe") == []
        assert await find_patients(session, crypto.indexer, name="Mallory") == [patient_id]
        assert await find_patients(session, crypto.indexer, identifier=SSN_SHAPED) == []
    async with engine.connect() as connection:
        rows = await connection.scalar(select(func.count()).select_from(PatientBlindIndex))
    assert rows == 2 + 1 + 1  # two name tokens, the birth date, one identifier


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE data_key SET kek_version = 9", id="update a key"),
        pytest.param("DELETE FROM data_key", id="delete a key"),
        pytest.param("TRUNCATE data_key", id="truncate keys"),
        pytest.param("UPDATE patient_blind_index SET kind = 'identifier'", id="update an index"),
        pytest.param("TRUNCATE patient_blind_index", id="truncate indexes"),
    ],
)
async def test_the_app_role_cannot_alter_keys_or_rewrite_indexes(
    engine: AsyncEngine, crypto: Crypto, statement: str
) -> None:
    await _create(engine, crypto, ABE)

    with pytest.raises(DBAPIError, match="permission denied"):
        async with _session(engine) as session:
            await session.execute(text(statement))


def test_no_production_code_constructs_the_test_sealer() -> None:
    app_directory = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.name
        for path in app_directory.rglob("*.py")
        if "LabelingTestSealer" in path.read_text() or "test-sealed" in path.read_text()
    ]

    assert offenders == []
