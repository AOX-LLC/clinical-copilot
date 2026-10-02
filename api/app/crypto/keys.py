"""The two secrets that live outside the database, read from the environment or a file.

``FIELD_KEK`` wraps every data key. ``BLIND_INDEX_KEY`` keys the HMAC lookup digests. They
must differ: one leaked key should not open the other's job. Each is 32 random bytes,
base64-encoded (``openssl rand -base64 32``), given either directly (``NAME``) or as the
path of a secret file (``NAME_FILE``). Errors name the variable, never its value.
"""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.crypto.cipher import KEY_BYTES
from app.crypto.errors import KeyMaterialError

KEK_VARIABLE = "FIELD_KEK"
KEK_VERSION_VARIABLE = "FIELD_KEK_VERSION"
BLIND_INDEX_VARIABLE = "BLIND_INDEX_KEY"
DEFAULT_KEK_VERSION = 1
MAX_KEK_VERSION = 32767  # a smallint column


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    kek: bytes = field(repr=False)
    kek_version: int
    blind_index_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.kek) != KEY_BYTES or len(self.blind_index_key) != KEY_BYTES:
            raise KeyMaterialError("both keys must be 32 bytes")
        if self.kek == self.blind_index_key:
            raise KeyMaterialError(f"{KEK_VARIABLE} and {BLIND_INDEX_VARIABLE} must differ")
        if not 1 <= self.kek_version <= MAX_KEK_VERSION:
            raise KeyMaterialError(f"{KEK_VERSION_VARIABLE} is out of range")


def load_key_material(environment: Mapping[str, str]) -> KeyMaterial:
    return KeyMaterial(
        kek=_read_key(environment, KEK_VARIABLE),
        kek_version=_read_version(environment),
        blind_index_key=_read_key(environment, BLIND_INDEX_VARIABLE),
    )


def _read_key(environment: Mapping[str, str], name: str) -> bytes:
    direct, file_path = environment.get(name), environment.get(f"{name}_FILE")
    if bool(direct) == bool(file_path):
        raise KeyMaterialError(f"set exactly one of {name} and {name}_FILE")
    encoded = direct if direct else _read_secret_file(name, file_path or "")
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeyMaterialError(f"{name} is not valid base64") from None
    if len(key) != KEY_BYTES:
        raise KeyMaterialError(f"{name} must decode to {KEY_BYTES} bytes")
    return key


def _read_secret_file(name: str, path: str) -> str:
    try:
        return Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise KeyMaterialError(f"{name}_FILE cannot be read") from None


def _read_version(environment: Mapping[str, str]) -> int:
    raw = environment.get(KEK_VERSION_VARIABLE)
    if not raw:
        return DEFAULT_KEK_VERSION
    try:
        return int(raw)
    except ValueError:
        raise KeyMaterialError(f"{KEK_VERSION_VARIABLE} must be an integer") from None
