"""AES-256-GCM for one field value.

A sealed value is ``version byte || 12-byte nonce || ciphertext || 16-byte tag``: 29 bytes
of overhead. The associated data is the table, column and row id the value lives in, so a
ciphertext copied to another row or column fails to open instead of decrypting to someone
else's data.

The version byte is the data-key generation (ADR 0014). Rotating a key-encryption key
re-wraps data keys and never changes a field ciphertext; a future re-key of a patient's data
would write version 2 beside version 1.
"""

import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.crypto.errors import DecryptionError

DATA_KEY_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
OVERHEAD_BYTES = 1 + NONCE_BYTES + TAG_BYTES
_FIELD_DOMAIN = b"clinical-copilot/field\x00"


def field_associated_data(table: str, column: str, row_id: uuid.UUID) -> bytes:
    return _FIELD_DOMAIN + f"{table}\x00{column}\x00{row_id}".encode()


def seal(key: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return bytes([DATA_KEY_VERSION]) + nonce + ciphertext


def open_sealed(key: bytes, sealed: bytes, associated_data: bytes) -> bytes:
    if len(sealed) < OVERHEAD_BYTES:
        raise DecryptionError("sealed value is too short")
    if sealed[0] != DATA_KEY_VERSION:
        raise DecryptionError(f"unknown data key version {sealed[0]}")
    nonce, ciphertext = sealed[1 : 1 + NONCE_BYTES], sealed[1 + NONCE_BYTES :]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data)
    except InvalidTag:
        raise DecryptionError("sealed value failed authentication") from None
