"""Data keys: wrapping them under the KEK, holding them in memory, and sealing with them.

Each patient has a random 32-byte data key. Records with no patient subject (practitioners,
organizations) share one system key (ADR 0008). Keys are stored wrapped under the KEK, bound
to their owner and KEK version, and unwrapped into a ``KeyRing`` for the length of a run.
``FieldSealer`` is the production ``PayloadSealer``: it picks the key from the seal context.
"""

import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.crypto import cipher
from app.crypto.cipher import KEY_BYTES, NONCE_BYTES
from app.crypto.errors import KeyUnavailableError
from app.timeline.ingest import SealContext

_WRAP_DOMAIN = b"clinical-copilot/wrap\x00"
SYSTEM_OWNER = "system"


def generate_data_key() -> bytes:
    return os.urandom(KEY_BYTES)


class KeyWrapper:
    """Wraps and unwraps data keys under one version of the key-encryption key."""

    def __init__(self, kek: bytes, kek_version: int) -> None:
        self._aead = AESGCM(kek)
        self.kek_version = kek_version

    def wrap(self, data_key: bytes, owner: uuid.UUID | None) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, data_key, self._associated_data(owner))

    def unwrap(self, wrapped: bytes, owner: uuid.UUID | None) -> bytes:
        nonce, ciphertext = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, self._associated_data(owner))
        except InvalidTag:
            raise KeyUnavailableError("a data key cannot be unwrapped with this key") from None

    def _associated_data(self, owner: uuid.UUID | None) -> bytes:
        label = SYSTEM_OWNER if owner is None else str(owner)
        return _WRAP_DOMAIN + label.encode() + b"\x00" + self.kek_version.to_bytes(2, "big")


def rewrap(wrapped: bytes, owner: uuid.UUID | None, old: KeyWrapper, new: KeyWrapper) -> bytes:
    """Move a data key from one KEK version to another. Field ciphertexts are untouched."""
    return new.wrap(old.unwrap(wrapped, owner), owner)


class KeyRing:
    """Unwrapped data keys for the current run, by owner (``None`` is the system key)."""

    def __init__(self) -> None:
        self._keys: dict[uuid.UUID | None, bytes] = {}

    def __contains__(self, owner: uuid.UUID | None) -> bool:
        return owner in self._keys

    def add(self, owner: uuid.UUID | None, key: bytes) -> None:
        self._keys[owner] = key

    def discard(self, owner: uuid.UUID | None) -> None:
        self._keys.pop(owner, None)

    def get(self, owner: uuid.UUID | None) -> bytes:
        try:
            return self._keys[owner]
        except KeyError:
            label = SYSTEM_OWNER if owner is None else f"patient {owner}"
            raise KeyUnavailableError(f"no data key is loaded for {label}") from None


class FieldSealer:
    """Seals and opens field values with the owner's data key. Implements ``PayloadSealer``."""

    def __init__(self, ring: KeyRing) -> None:
        self._ring = ring

    def seal(self, plaintext: bytes, context: SealContext) -> bytes:
        key = self._ring.get(context.patient_id)
        return cipher.seal(key, plaintext, _associated_data(context))

    def open(self, sealed: bytes, context: SealContext) -> bytes:
        key = self._ring.get(context.patient_id)
        return cipher.open_sealed(key, sealed, _associated_data(context))


def _associated_data(context: SealContext) -> bytes:
    return cipher.field_associated_data(context.table, context.column, context.row_id)
