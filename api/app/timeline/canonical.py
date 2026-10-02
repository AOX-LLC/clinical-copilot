"""Canonical form and content hash of a source payload.

Two payloads hash equal exactly when they carry the same clinical content. The
canonical form is JSON with sorted keys and no insignificant whitespace, with the
metadata a server assigns on every write removed, so reloading identical content into
a reset FHIR server does not look like a new version.

Number tokens keep their source text: in FHIR, ``1.0`` and ``1`` state different
precision, so they must not collapse to the same hash.

Only the hash uses this form. The payload itself is stored exactly as received.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.timeline.vocabulary import SourceKind

FHIR_SERVER_ASSIGNED_META_KEYS = frozenset({"versionId", "lastUpdated", "source"})
HEALTHIE_SERVER_ASSIGNED_KEYS = frozenset({"updated_at"})


class CanonicalizationError(ValueError):
    """The payload is not a JSON object we can hash unambiguously."""


@dataclass(frozen=True, slots=True)
class _NumberToken:
    text: str


def content_sha256(payload: bytes, source_kind: SourceKind) -> bytes:
    """Return the SHA-256 digest of the payload's canonical form."""
    return hashlib.sha256(canonical_json(payload, source_kind)).digest()


def canonical_json(payload: bytes, source_kind: SourceKind) -> bytes:
    document = _parse_object(payload)
    stripped = _STRIPPERS[source_kind](document)
    try:
        return _serialize(stripped).encode("utf-8")
    except RecursionError as error:
        raise CanonicalizationError("payload is nested too deeply") from error
    except UnicodeEncodeError as error:
        raise CanonicalizationError("payload contains an unpaired surrogate") from error


def _parse_object(payload: bytes) -> dict[str, Any]:
    try:
        # Decode strictly first: json.loads would also accept UTF-16 and a BOM.
        text = payload.decode("utf-8")
        document = json.loads(
            text,
            parse_float=_NumberToken,
            parse_int=_NumberToken,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanonicalizationError("payload is not valid UTF-8 JSON") from error
    except RecursionError as error:
        raise CanonicalizationError("payload is nested too deeply") from error
    if not isinstance(document, dict):
        raise CanonicalizationError("payload must be a JSON object")
    return document


def _reject_constant(name: str) -> Any:
    raise CanonicalizationError(f"non-standard JSON constant {name}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(pairs)
    if len(document) != len(pairs):
        # Parsers disagree on which duplicate wins, so the content is ambiguous.
        raise CanonicalizationError("payload has a duplicate object key")
    return document


def _strip_fhir_server_metadata(resource: dict[str, Any]) -> dict[str, Any]:
    meta = resource.get("meta")
    if not isinstance(meta, dict):
        return resource
    kept_meta = {
        key: value for key, value in meta.items() if key not in FHIR_SERVER_ASSIGNED_META_KEYS
    }
    without_meta = {key: value for key, value in resource.items() if key != "meta"}
    if kept_meta:
        without_meta["meta"] = kept_meta
    return without_meta


def _strip_healthie_server_metadata(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in document.items() if key not in HEALTHIE_SERVER_ASSIGNED_KEYS
    }


def _keep_everything(document: dict[str, Any]) -> dict[str, Any]:
    return document


_STRIPPERS: dict[SourceKind, Callable[[dict[str, Any]], dict[str, Any]]] = {
    SourceKind.FHIR_R4: _strip_fhir_server_metadata,
    SourceKind.HEALTHIE: _strip_healthie_server_metadata,
    SourceKind.LAB_FEED: _keep_everything,
}


def _serialize(value: Any) -> str:
    match value:
        case dict():
            members = (
                f"{json.dumps(key, ensure_ascii=False)}:{_serialize(value[key])}"
                for key in sorted(value)
            )
            return "{" + ",".join(members) + "}"
        case list():
            return "[" + ",".join(_serialize(item) for item in value) + "]"
        case bool() | None:
            return json.dumps(value)
        case str():
            return json.dumps(value, ensure_ascii=False)
        case _NumberToken(text=text):
            return text
    raise CanonicalizationError(f"unexpected JSON value of type {type(value).__name__}")
