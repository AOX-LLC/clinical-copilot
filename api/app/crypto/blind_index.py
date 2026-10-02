"""HMAC blind indexes: exact-match lookup on values that are stored encrypted.

A digest is HMAC-SHA256 over a kind label and a normalized value, under a key that is not
the KEK. The database holds digests only, so it can answer "which patients carry this name
token" without holding a name. Matching is exact: no prefix, no fuzzy search, and a name
search needs every token it was given.

Names are split into letter runs, folded to NFKC lower case and stripped of digits, so
``Abe604`` and ``abe`` meet. Synthea appends digits to names; real names do not carry them.
"""

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Iterable
from datetime import date
from enum import StrEnum

_LETTER_RUN = re.compile(r"[^\W\d_]+")


class IndexKind(StrEnum):
    NAME_TOKEN = "name_token"  # noqa: S105  # an index kind, not a credential
    BIRTH_DATE = "birth_date"
    IDENTIFIER = "identifier"


class BlindIndexer:
    def __init__(self, key: bytes) -> None:
        self._key = key

    def name_tokens(self, names: Iterable[str]) -> set[bytes]:
        tokens = {
            token
            for name in names
            for token in _LETTER_RUN.findall(unicodedata.normalize("NFKC", name).casefold())
        }
        return {self._digest(IndexKind.NAME_TOKEN, token) for token in tokens}

    def birth_date(self, value: date) -> bytes:
        return self._digest(IndexKind.BIRTH_DATE, value.isoformat())

    def identifier(self, system: str, value: str) -> bytes:
        normalized = f"{system.strip()}|{unicodedata.normalize('NFKC', value).strip().casefold()}"
        return self._digest(IndexKind.IDENTIFIER, normalized)

    def _digest(self, kind: IndexKind, normalized: str) -> bytes:
        message = f"{kind.value}\x00{normalized}".encode()
        return hmac.new(self._key, message, hashlib.sha256).digest()
