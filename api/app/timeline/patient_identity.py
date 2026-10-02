"""A patient's identity: sealed into the patient row, findable by blind index.

Name, birth date and identifiers go into ``_enc`` columns, sealed with the patient's own data
key and bound to the patient row. The same values are digested into ``patient_blind_index``,
which is what exact-match lookup reads: the database never holds a name in the clear.
A patient is inserted with no encrypted fields first, because a data key refers to the
patient row; the fields are sealed and written in the same transaction.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto.blind_index import BlindIndexer, IndexKind
from app.crypto.keyring import FieldSealer
from app.crypto.keystore import KeyStore
from app.timeline.ingest import SealContext
from app.timeline.models import Patient, PatientBlindIndex, PatientSourceLink

TABLE = "patient"


@dataclass(frozen=True, slots=True)
class PatientIdentifier:
    system: str = field(repr=False)
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class PatientIdentity:
    given_names: tuple[str, ...]
    family_name: str | None
    birth_date: date | None
    identifiers: tuple[PatientIdentifier, ...] = ()
    sex_at_birth: str = "unknown"

    def __repr__(self) -> str:
        return "PatientIdentity(<redacted>)"


async def create_patient(
    session: AsyncSession,
    identity: PatientIdentity,
    keystore: KeyStore,
    sealer: FieldSealer,
    indexer: BlindIndexer,
) -> uuid.UUID:
    patient_id = uuid.uuid4()
    await session.execute(insert(Patient).values(id=patient_id, sex_at_birth=identity.sex_at_birth))
    await keystore.load(session, [patient_id])
    await replace_identity(session, patient_id, identity, sealer, indexer)
    return patient_id


async def resolve_source_patient(
    session: AsyncSession,
    source_system_id: int,
    external_id: str,
    identity: PatientIdentity,
    keystore: KeyStore,
    sealer: FieldSealer,
    indexer: BlindIndexer,
) -> tuple[uuid.UUID, bool]:
    """The patient a source's patient id is linked to, created and linked on first sight.

    Resolution is by source link only. Two source patients with the same name and birth date
    stay two patients: matching across sources is a policy decision that belongs with the
    second source, and a wrong merge puts one person's record on another's chart.
    A new patient is sealed from ``identity``; an existing one is left exactly as it is,
    because sealing again would change every ciphertext for no change in content. The
    caller refreshes the identity with ``replace_identity`` when the source's patient
    record changes. The patient's data key is loaded into the ring either way.
    Returns the patient id and whether it was just created.
    """
    existing = await session.scalar(
        select(PatientSourceLink.patient_id).where(
            PatientSourceLink.source_system_id == source_system_id,
            PatientSourceLink.external_id == external_id,
        )
    )
    if existing is not None:
        await keystore.load(session, [existing])
        return existing, False

    patient_id = await create_patient(session, identity, keystore, sealer, indexer)
    linked = await session.scalar(
        insert(PatientSourceLink)
        .values(source_system_id=source_system_id, external_id=external_id, patient_id=patient_id)
        .on_conflict_do_nothing()
        .returning(PatientSourceLink.patient_id)
    )
    if linked is None:
        # Another run linked this source patient first; this transaction must not keep a duplicate.
        raise LinkRaceError(f"source patient {external_id} was linked by another run")
    return patient_id, True


class LinkRaceError(Exception):
    """Two runs tried to link the same source patient; the caller's transaction should roll back."""


async def replace_identity(
    session: AsyncSession,
    patient_id: uuid.UUID,
    identity: PatientIdentity,
    sealer: FieldSealer,
    indexer: BlindIndexer,
) -> None:
    """Seal the identity into the patient row and rebuild its blind indexes."""

    def seal(column: str, text: str | None) -> bytes | None:
        if text is None:
            return None
        context = _context(column, patient_id)
        return sealer.seal(text.encode("utf-8"), context)

    pairs = [[item.system, item.value] for item in identity.identifiers]
    await session.execute(
        update(Patient)
        .where(Patient.id == patient_id)
        .values(
            given_name_enc=seal(
                "given_name_enc",
                json.dumps(list(identity.given_names)) if identity.given_names else None,
            ),
            family_name_enc=seal("family_name_enc", identity.family_name),
            birth_date_enc=seal(
                "birth_date_enc", identity.birth_date.isoformat() if identity.birth_date else None
            ),
            identifiers_enc=seal("identifiers_enc", json.dumps(pairs) if pairs else None),
            sex_at_birth=identity.sex_at_birth,
        )
    )
    await session.execute(
        delete(PatientBlindIndex).where(PatientBlindIndex.patient_id == patient_id)
    )
    digests = _digests(identity, indexer)
    if digests:
        await session.execute(
            insert(PatientBlindIndex).values(
                [
                    {"kind": kind, "digest": digest, "patient_id": patient_id}
                    for kind, digest in digests
                ]
            )
        )


async def read_identity(
    session: AsyncSession, patient_id: uuid.UUID, sealer: FieldSealer
) -> PatientIdentity:
    row = (await session.execute(select(Patient).where(Patient.id == patient_id))).scalar_one()

    def open_column(column: str, sealed: bytes | None) -> str | None:
        if sealed is None:
            return None
        return sealer.open(sealed, _context(column, patient_id)).decode("utf-8")

    given = open_column("given_name_enc", row.given_name_enc)
    birth = open_column("birth_date_enc", row.birth_date_enc)
    identifiers = open_column("identifiers_enc", row.identifiers_enc)
    return PatientIdentity(
        given_names=tuple(json.loads(given)) if given else (),
        family_name=open_column("family_name_enc", row.family_name_enc),
        birth_date=date.fromisoformat(birth) if birth else None,
        identifiers=tuple(PatientIdentifier(s, v) for s, v in json.loads(identifiers or "[]")),
        sex_at_birth=row.sex_at_birth or "unknown",
    )


async def find_patients(
    session: AsyncSession,
    indexer: BlindIndexer,
    *,
    name: str | None = None,
    birth_date: date | None = None,
    identifier: PatientIdentifier | None = None,
) -> list[uuid.UUID]:
    """Patients matching every criterion given, exactly. A name needs all of its tokens."""
    wanted: set[tuple[str, bytes]] = set()
    if name is not None:
        tokens = indexer.name_tokens([name])
        if not tokens:
            return []
        wanted |= {(IndexKind.NAME_TOKEN.value, token) for token in tokens}
    if birth_date is not None:
        wanted.add((IndexKind.BIRTH_DATE.value, indexer.birth_date(birth_date)))
    if identifier is not None:
        wanted.add(
            (IndexKind.IDENTIFIER.value, indexer.identifier(identifier.system, identifier.value))
        )
    if not wanted:
        raise ValueError("give at least one thing to look up")
    matched = await session.scalars(
        select(PatientBlindIndex.patient_id)
        .where(tuple_(PatientBlindIndex.kind, PatientBlindIndex.digest).in_(wanted))
        .group_by(PatientBlindIndex.patient_id)
        .having(func.count() == len(wanted))
        .order_by(PatientBlindIndex.patient_id)
    )
    return list(matched)


def _context(column: str, patient_id: uuid.UUID) -> SealContext:
    """Where a patient's value lives: their own row, sealed under their own key."""
    return SealContext(table=TABLE, column=column, row_id=patient_id, patient_id=patient_id)


def _digests(identity: PatientIdentity, indexer: BlindIndexer) -> set[tuple[str, bytes]]:
    names = [*identity.given_names, *([identity.family_name] if identity.family_name else [])]
    digests = {(IndexKind.NAME_TOKEN.value, token) for token in indexer.name_tokens(names)}
    if identity.birth_date is not None:
        digests.add((IndexKind.BIRTH_DATE.value, indexer.birth_date(identity.birth_date)))
    for item in identity.identifiers:
        digests.add((IndexKind.IDENTIFIER.value, indexer.identifier(item.system, item.value)))
    return digests
