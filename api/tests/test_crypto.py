"""Field encryption: the cipher, key handling and blind indexes. Synthetic data only."""

import base64
import os
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.crypto import cipher
from app.crypto.blind_index import BlindIndexer, IndexKind
from app.crypto.errors import DecryptionError, KeyMaterialError, KeyUnavailableError
from app.crypto.keyring import FieldSealer, KeyRing, KeyWrapper, generate_data_key, rewrap
from app.crypto.keys import KeyMaterial, load_key_material
from app.timeline.ingest import SealContext

PATIENT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
PATIENT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
ROW_ONE = uuid.UUID("11111111-0000-4000-8000-000000000001")
ROW_TWO = uuid.UUID("11111111-0000-4000-8000-000000000002")
SENTINEL = "Quillfeather-Sentinel"
# "ABE" in fullwidth letters, built from code points so no tool rewrites it.
FULLWIDTH_ABE = "".join(chr(code) for code in (0xFF21, 0xFF22, 0xFF25))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _sealer(*owners: uuid.UUID | None) -> FieldSealer:
    ring = KeyRing()
    for owner in owners:
        ring.add(owner, generate_data_key())
    return FieldSealer(ring)


def _context(
    row: uuid.UUID = ROW_ONE,
    column: str = "family_name_enc",
    table: str = "patient",
    patient: uuid.UUID | None = PATIENT_A,
) -> SealContext:
    return SealContext(table, column, row, patient)


def test_a_sealed_value_opens_to_what_was_sealed() -> None:
    sealer = _sealer(PATIENT_A)

    sealed = sealer.seal(SENTINEL.encode(), _context())

    assert sealer.open(sealed, _context()) == SENTINEL.encode()


def test_a_sealed_value_hides_its_plaintext_and_costs_29_bytes() -> None:
    sealer = _sealer(PATIENT_A)

    sealed = sealer.seal(SENTINEL.encode(), _context())

    assert SENTINEL.encode() not in sealed
    assert len(sealed) == len(SENTINEL) + cipher.OVERHEAD_BYTES == len(SENTINEL) + 29
    assert sealed[0] == cipher.DATA_KEY_VERSION


def test_sealing_the_same_value_twice_gives_different_ciphertext() -> None:
    sealer = _sealer(PATIENT_A)

    first = sealer.seal(b"same", _context())
    second = sealer.seal(b"same", _context())

    assert first != second


@pytest.mark.parametrize(
    ("sealed_in", "opened_in"),
    [
        pytest.param(_context(row=ROW_ONE), _context(row=ROW_TWO), id="another row"),
        pytest.param(
            _context(column="given_name_enc"),
            _context(column="family_name_enc"),
            id="another column",
        ),
        pytest.param(
            _context(table="patient"), _context(table="timeline_event"), id="another table"
        ),
    ],
)
def test_ciphertext_moved_to_another_row_column_or_table_does_not_open(
    sealed_in: SealContext, opened_in: SealContext
) -> None:
    sealer = _sealer(PATIENT_A)
    sealed = sealer.seal(SENTINEL.encode(), sealed_in)

    with pytest.raises(DecryptionError):
        sealer.open(sealed, opened_in)


def test_ciphertext_does_not_open_under_another_patients_key() -> None:
    ring = KeyRing()
    ring.add(PATIENT_A, generate_data_key())
    ring.add(PATIENT_B, generate_data_key())
    sealer = FieldSealer(ring)
    sealed = sealer.seal(SENTINEL.encode(), _context(patient=PATIENT_A))

    with pytest.raises(DecryptionError):
        sealer.open(sealed, _context(patient=PATIENT_B))


def test_a_destroyed_patient_key_makes_only_that_patients_data_unreadable() -> None:
    ring = KeyRing()
    ring.add(PATIENT_A, generate_data_key())
    ring.add(PATIENT_B, generate_data_key())
    sealer = FieldSealer(ring)
    sealed_a = sealer.seal(b"a", _context(patient=PATIENT_A))
    sealed_b = sealer.seal(b"b", _context(patient=PATIENT_B))

    surviving_ring = KeyRing()
    surviving_ring.add(PATIENT_B, ring.get(PATIENT_B))  # patient A's key is gone
    after_destruction = FieldSealer(surviving_ring)

    with pytest.raises(KeyUnavailableError, match="patient aaaaaaaa"):
        after_destruction.open(sealed_a, _context(patient=PATIENT_A))
    with pytest.raises(KeyUnavailableError):
        after_destruction.seal(b"new", _context(patient=PATIENT_A))
    assert after_destruction.open(sealed_b, _context(patient=PATIENT_B)) == b"b"


def test_records_with_no_patient_use_the_system_key() -> None:
    sealer = _sealer(None, PATIENT_A)
    system_context = _context(table="source_record", column="payload_enc", patient=None)

    sealed = sealer.seal(b"practitioner", system_context)

    assert sealer.open(sealed, system_context) == b"practitioner"
    with pytest.raises(DecryptionError):
        sealer.open(sealed, _context(table="source_record", column="payload_enc"))


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda s: s[:-1] + bytes([s[-1] ^ 1]), id="flipped tag bit"),
        pytest.param(lambda s: s[:14] + bytes([s[14] ^ 1]) + s[15:], id="flipped body bit"),
        pytest.param(lambda s: s[:3] + bytes([s[3] ^ 1]) + s[4:], id="flipped nonce bit"),
        pytest.param(lambda s: s[:-1], id="truncated"),
        pytest.param(lambda s: s + b"\x00", id="extended"),
    ],
)
def test_an_altered_value_does_not_open(tamper: object) -> None:
    sealer = _sealer(PATIENT_A)
    sealed = sealer.seal(SENTINEL.encode(), _context())

    with pytest.raises(DecryptionError):
        sealer.open(tamper(sealed), _context())  # type: ignore[operator]


def test_an_unknown_version_byte_or_a_short_value_does_not_open() -> None:
    sealer = _sealer(PATIENT_A)
    sealed = sealer.seal(b"x", _context())

    with pytest.raises(DecryptionError, match="version 2"):
        sealer.open(bytes([2]) + sealed[1:], _context())
    with pytest.raises(DecryptionError, match="too short"):
        sealer.open(b"\x01short", _context())


def test_errors_never_carry_plaintext_or_key_bytes() -> None:
    sealer = _sealer(PATIENT_A)
    sealed = sealer.seal(SENTINEL.encode(), _context())
    messages = []
    for context in (_context(row=ROW_TWO), _context(patient=PATIENT_B)):
        with pytest.raises((DecryptionError, KeyUnavailableError)) as raised:
            sealer.open(sealed, context)
        messages.append(str(raised.value) + repr(raised.value))

    assert not [message for message in messages if SENTINEL in message]


def test_a_wrapped_key_unwraps_only_for_its_owner_and_kek_version() -> None:
    kek = os.urandom(32)
    wrapper = KeyWrapper(kek, kek_version=1)
    data_key = generate_data_key()
    wrapped = wrapper.wrap(data_key, PATIENT_A)

    assert wrapper.unwrap(wrapped, PATIENT_A) == data_key
    assert data_key not in wrapped
    with pytest.raises(KeyUnavailableError):
        wrapper.unwrap(wrapped, PATIENT_B)
    with pytest.raises(KeyUnavailableError):
        wrapper.unwrap(wrapped, None)
    with pytest.raises(KeyUnavailableError):
        KeyWrapper(kek, kek_version=2).unwrap(wrapped, PATIENT_A)
    with pytest.raises(KeyUnavailableError):
        KeyWrapper(os.urandom(32), kek_version=1).unwrap(wrapped, PATIENT_A)


def test_rotating_the_kek_rewraps_the_data_key_and_leaves_field_ciphertext_alone() -> None:
    old, new = KeyWrapper(os.urandom(32), 1), KeyWrapper(os.urandom(32), 2)
    data_key = generate_data_key()
    ring = KeyRing()
    ring.add(PATIENT_A, data_key)
    sealed = FieldSealer(ring).seal(SENTINEL.encode(), _context())

    moved = rewrap(old.wrap(data_key, PATIENT_A), PATIENT_A, old, new)

    unwrapped = new.unwrap(moved, PATIENT_A)
    reopened = KeyRing()
    reopened.add(PATIENT_A, unwrapped)
    assert FieldSealer(reopened).open(sealed, _context()) == SENTINEL.encode()
    with pytest.raises(KeyUnavailableError):
        old.unwrap(moved, PATIENT_A)


def _environment(kek: bytes | None = None, blind: bytes | None = None) -> dict[str, str]:
    return {
        "FIELD_KEK": _b64(kek if kek is not None else os.urandom(32)),
        "BLIND_INDEX_KEY": _b64(blind if blind is not None else os.urandom(32)),
    }


def test_key_material_loads_from_the_environment() -> None:
    kek, blind = os.urandom(32), os.urandom(32)

    material = load_key_material(_environment(kek, blind))

    assert (material.kek, material.blind_index_key, material.kek_version) == (kek, blind, 1)


def test_key_material_loads_from_secret_files(tmp_path: Path) -> None:
    kek_file, blind_file = tmp_path / "kek", tmp_path / "blind"
    kek, blind = os.urandom(32), os.urandom(32)
    kek_file.write_text(_b64(kek) + "\n")
    blind_file.write_text(_b64(blind))

    material = load_key_material(
        {"FIELD_KEK_FILE": str(kek_file), "BLIND_INDEX_KEY_FILE": str(blind_file)}
        | {"FIELD_KEK_VERSION": "7"}
    )

    assert (material.kek, material.blind_index_key, material.kek_version) == (kek, blind, 7)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        pytest.param(lambda env: env.pop("FIELD_KEK"), "exactly one of FIELD_KEK", id="no kek"),
        pytest.param(
            lambda env: env.update(FIELD_KEK_FILE="/nonexistent/kek"),
            "exactly one of FIELD_KEK",
            id="both forms",
        ),
        pytest.param(
            lambda env: env.update(FIELD_KEK="not base64!"), "not valid base64", id="text"
        ),
        pytest.param(
            lambda env: env.update(BLIND_INDEX_KEY=_b64(b"short")), "must decode to 32", id="short"
        ),
        pytest.param(
            lambda env: env.update(FIELD_KEK_VERSION="two"), "must be an integer", id="version"
        ),
        pytest.param(
            lambda env: env.update(FIELD_KEK_VERSION="0"), "out of range", id="version range"
        ),
    ],
)
def test_bad_key_material_is_refused(mutation: object, message: str) -> None:
    environment = _environment()
    mutation(environment)  # type: ignore[operator]

    with pytest.raises(KeyMaterialError, match=message):
        load_key_material(environment)


def test_the_kek_and_the_blind_index_key_must_differ() -> None:
    shared = os.urandom(32)

    with pytest.raises(KeyMaterialError, match="must differ"):
        load_key_material(_environment(kek=shared, blind=shared))


def test_an_unreadable_secret_file_is_refused_without_naming_its_path(tmp_path: Path) -> None:
    environment = {
        "FIELD_KEK_FILE": str(tmp_path / "missing-kek"),
        "BLIND_INDEX_KEY": _b64(os.urandom(32)),
    }

    with pytest.raises(KeyMaterialError) as raised:
        load_key_material(environment)

    assert "FIELD_KEK_FILE" in str(raised.value)
    assert "missing-kek" not in str(raised.value)


def test_key_errors_and_reprs_never_contain_key_material() -> None:
    secret = "SECRET-VALUE-0123456789"
    with pytest.raises(KeyMaterialError) as raised:
        load_key_material({"FIELD_KEK": secret, "BLIND_INDEX_KEY": _b64(os.urandom(32))})
    material = KeyMaterial(os.urandom(32), 1, os.urandom(32))

    assert secret not in str(raised.value)
    assert material.kek.hex() not in repr(material)
    assert _b64(material.kek) not in repr(material)


def test_a_digest_depends_on_the_key_and_on_the_kind() -> None:
    first, second = BlindIndexer(os.urandom(32)), BlindIndexer(os.urandom(32))

    assert first.birth_date(date(1970, 1, 1)) == first.birth_date(date(1970, 1, 1))
    assert first.birth_date(date(1970, 1, 1)) != second.birth_date(date(1970, 1, 1))
    assert len(first.birth_date(date(1970, 1, 1))) == 32
    assert first._digest(IndexKind.NAME_TOKEN, "x") != first._digest(IndexKind.IDENTIFIER, "x")


def test_name_tokens_ignore_case_digits_and_punctuation() -> None:
    indexer = BlindIndexer(os.urandom(32))

    synthetic = indexer.name_tokens(["Abe604", "Dickens475"])

    assert synthetic == indexer.name_tokens(["abe", "DICKENS"])
    assert indexer.name_tokens(["O'Brien-Smith"]) == indexer.name_tokens(["o", "brien", "smith"])
    assert indexer.name_tokens([FULLWIDTH_ABE]) == indexer.name_tokens(["abe"])
    assert indexer.name_tokens(["", "42", "  "]) == set()


def test_a_near_miss_name_token_does_not_match() -> None:
    indexer = BlindIndexer(os.urandom(32))

    assert not indexer.name_tokens(["Dickens"]) & indexer.name_tokens(["Dickenson"])


def test_identifiers_are_matched_by_system_and_value_ignoring_case() -> None:
    indexer = BlindIndexer(os.urandom(32))

    assert indexer.identifier("http://example.test/mrn", "AB-12") == indexer.identifier(
        "http://example.test/mrn", " ab-12 "
    )
    assert indexer.identifier("http://example.test/mrn", "AB-12") != indexer.identifier(
        "http://example.test/other", "AB-12"
    )
