"""The FHIR R4 adapter.

It does transport and identity only: it fetches resources from a FHIR R4 server and says
which resource and version each one is. Normalizers turn them into timeline rows.

What the server cannot do is declared, not faked (ADR 0013): it has no exact-version reads
(``supports_versions``), cannot answer "changed after" reliably (``supports_since``), and
does not page. So when a listing starts, the adapter reads the whole result once, keeps
it briefly in memory, and serves the pages from that snapshot: the work is linear, and pages
cannot skip or repeat records if the source changes mid-listing. A cursor whose snapshot has
expired is a retryable error. Search results are re-serialized compactly with every number
token and key kept, which hashes the same as the bytes a direct read returns.

Nothing here puts payload content into an exception message, a log line or a ``repr``.
"""

import base64
import binascii
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx

from app.ehr.ports import (
    AdapterCapabilities,
    ApprovedSummaryDocument,
    ChangeNotification,
    OperationNotSupportedError,
    Page,
    PermanentSourceError,
    RateLimitedError,
    RecordKind,
    RecordNotFoundError,
    RetryableSourceError,
    SourcePatient,
    SourceRecord,
    SourceSystemRef,
    WriteBackReceipt,
)

CHANGES_PAGE_SIZE = 100
MAX_RETRY_AFTER_SECONDS = 300.0
# Memory bounds: one response, and the records one listing keeps. Four listings may be held at
# once, so the worst case stays well inside the API container's memory limit.
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 24 * 1024 * 1024
SNAPSHOT_SECONDS = 300.0
MAX_SNAPSHOTS = 4
REMEMBERED_ENDINGS = 64

# The FHIR resource types behind each record kind this adapter can fetch.
RESOURCE_TYPES_BY_KIND: dict[RecordKind, tuple[str, ...]] = {
    RecordKind.PATIENT: ("Patient",),
    RecordKind.ENCOUNTER: ("Encounter",),
    RecordKind.CONDITION: ("Condition",),
    RecordKind.OBSERVATION: ("Observation",),
    RecordKind.MEDICATION: ("MedicationRequest", "MedicationStatement"),
    RecordKind.CARE_PLAN: ("CarePlan",),
    RecordKind.PROCEDURE: ("Procedure",),
    RecordKind.IMMUNIZATION: ("Immunization",),
    RecordKind.ALLERGY: ("AllergyIntolerance",),
}

READ_RESOURCE_TYPES = frozenset(
    resource_type for types in RESOURCE_TYPES_BY_KIND.values() for resource_type in types
)
# A FHIR id may contain dots, but "." and ".." are path segments: httpx collapses them, so
# ``Patient/.`` would become the search-all request ``Patient``.
_FHIR_ID = re.compile(r"(?!\.+$)[A-Za-z0-9\-.]{1,64}")
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{32}")
_ENDING_MESSAGES = {
    "expired": f"the paging snapshot expired after {SNAPSHOT_SECONDS:.0f} s unused; "
    "start the listing again",
    "evicted": "the paging snapshot was evicted by newer listings; start the listing again",
    "unknown": "no paging snapshot matches this cursor (the listing finished, or the cursor "
    "was never issued); start the listing again",
}
_now = time.monotonic


@dataclass(slots=True)
class _Snapshot:
    key: tuple[object, ...]
    items: list[Any]
    last_used: float


class _Number(str):
    """A JSON number kept as its source text, so ``1.50`` is not rewritten as ``1.5``."""

    __slots__ = ()


class FhirR4Adapter:
    def __init__(self, source: SourceSystemRef, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client
        self._snapshots: dict[str, _Snapshot] = {}
        self._endings: dict[str, str] = {}  # why recent snapshots are gone, for the error message

    @property
    def source(self) -> SourceSystemRef:
        return self._source

    async def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            record_kinds=frozenset(RESOURCE_TYPES_BY_KIND),
            supports_versions=False,
            supports_write_back=False,
            supports_notifications=False,
            supports_since=False,
        )

    async def list_patients(self, cursor: str | None, page_size: int) -> Page[SourcePatient]:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        async def load() -> list[SourcePatient]:
            found = sorted(await self._search("Patient", {}), key=lambda item: item["id"])
            return [SourcePatient(item["id"], self._record_of(item)) for item in found]

        return await self._page_of(("patients",), load, cursor, page_size)

    async def fetch_changes(
        self,
        patient_external_id: str,
        kinds: frozenset[RecordKind],
        since: datetime | None,
        cursor: str | None,
    ) -> Page[SourceRecord]:
        """Return every record of the kinds for the patient; ``since`` is ignored (ADR 0013)."""
        _require_fhir_id("Patient", patient_external_id)
        wanted = sorted(kinds & RESOURCE_TYPES_BY_KIND.keys())

        async def load() -> list[SourceRecord]:
            records: list[SourceRecord] = []
            for kind in wanted:
                for resource_type in RESOURCE_TYPES_BY_KIND[kind]:
                    records.extend(await self._records_of_type(resource_type, patient_external_id))
            records.sort(key=lambda record: (record.resource_type, record.resource_id))
            return records

        key = ("changes", patient_external_id, tuple(wanted))
        return await self._page_of(key, load, cursor, CHANGES_PAGE_SIZE)

    async def get_record(
        self, resource_type: str, resource_id: str, version_id: str | None = None
    ) -> SourceRecord:
        _require_resource_type(resource_type)
        _require_fhir_id(resource_type, resource_id)
        what = f"{resource_type}/{resource_id}"
        body = await self._get(f"/{what}", {}, what)
        resource = _parse_json(body)
        if resource.get("resourceType") != resource_type or resource.get("id") != resource_id:
            raise PermanentSourceError(f"{what}: the source returned a different resource")
        current_version = _meta(resource).get("versionId")
        if version_id is not None and version_id != current_version:
            raise OperationNotSupportedError(
                f"{resource_type}/{resource_id}: the server cannot return version {version_id}"
            )
        return self._record_of(resource, payload=body)

    async def write_back(
        self, document: ApprovedSummaryDocument, idempotency_key: str
    ) -> WriteBackReceipt:
        raise OperationNotSupportedError("the FHIR adapter does not write back yet")

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ChangeNotification]:
        raise OperationNotSupportedError("the FHIR server sends no change notifications")

    async def _page_of[T](
        self,
        key: tuple[object, ...],
        load: Callable[[], Awaitable[list[T]]],
        cursor: str | None,
        page_size: int,
    ) -> Page[T]:
        """One page of a listing, from the snapshot its first call took."""
        if cursor is None:
            snapshot_id, offset = uuid4().hex, 0
            items = await load()
            if _payload_bytes(items) > MAX_SNAPSHOT_BYTES:
                raise PermanentSourceError("the listing is larger than this adapter will hold")
            self._remember(snapshot_id, _Snapshot(key, items, _now()))
        else:
            snapshot_id, offset = _decode_cursor(cursor)
            items = self._recall(snapshot_id, key).items
        page = items[offset : offset + page_size]
        next_offset = offset + len(page)
        if next_offset >= len(items):
            self._snapshots.pop(snapshot_id, None)
            return Page(items=tuple(page), next_cursor=None)
        return Page(items=tuple(page), next_cursor=_encode_cursor(snapshot_id, next_offset))

    def _remember(self, snapshot_id: str, snapshot: _Snapshot) -> None:
        """Keep a new snapshot, dropping idle ones first and then the least recently used."""
        for known_id, known in list(self._snapshots.items()):
            if snapshot.last_used - known.last_used >= SNAPSHOT_SECONDS:
                self._end(known_id, "expired")
        while len(self._snapshots) >= MAX_SNAPSHOTS:
            least_recent = min(self._snapshots, key=lambda known: self._snapshots[known].last_used)
            self._end(least_recent, "evicted")
        self._snapshots[snapshot_id] = snapshot

    def _recall(self, snapshot_id: str, key: tuple[object, ...]) -> _Snapshot:
        """The snapshot for a cursor; using it renews its lifetime, so a slow walk survives."""
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is not None and _now() - snapshot.last_used >= SNAPSHOT_SECONDS:
            self._end(snapshot_id, "expired")
            snapshot = None
        if snapshot is None:
            raise RetryableSourceError(_ENDING_MESSAGES[self._endings.get(snapshot_id, "unknown")])
        if snapshot.key != key:
            raise PermanentSourceError("cursor belongs to a different listing")
        snapshot.last_used = _now()
        return snapshot

    def _end(self, snapshot_id: str, reason: str) -> None:
        self._snapshots.pop(snapshot_id, None)
        self._endings[snapshot_id] = reason
        while len(self._endings) > REMEMBERED_ENDINGS:
            del self._endings[next(iter(self._endings))]

    async def _records_of_type(self, resource_type: str, patient_id: str) -> list[SourceRecord]:
        if resource_type == "Patient":
            return [await self.get_record("Patient", patient_id)]
        found = await self._search(resource_type, {"patient": patient_id})
        return [self._record_of(resource) for resource in found]

    async def _search(
        self, resource_type: str, parameters: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        # No _count: fhir-candle truncates without a next link, and returns everything without it.
        body = await self._get(f"/{resource_type}", parameters, resource_type)
        return _resources_of(_parse_json(body), resource_type)

    async def _get(self, path: str, parameters: Mapping[str, str], what: str) -> bytes:
        """The body of a successful response, read in chunks and refused past the size cap."""
        try:
            async with self._client.stream(
                "GET", path, params=parameters, headers={"Accept": "application/fhir+json"}
            ) as response:
                _checked(response, what)
                return await _read_capped(response, what)
        except httpx.TransportError as error:
            reason = type(error).__name__
            raise RetryableSourceError(f"{what}: transport error ({reason})") from None

    def _record_of(self, resource: dict[str, Any], payload: bytes | None = None) -> SourceRecord:
        meta = _meta(resource)
        return SourceRecord.from_payload(
            system=self._source,
            resource_type=resource["resourceType"],
            resource_id=resource["id"],
            version_id=meta.get("versionId"),
            source_updated_at=_instant(meta.get("lastUpdated")),
            payload=payload if payload is not None else _dump(resource).encode("utf-8"),
        )


def _encode_cursor(snapshot_id: str, offset: int) -> str:
    return base64.urlsafe_b64encode(f"snapshot:{snapshot_id}:{offset}".encode()).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        label, snapshot_id, offset_text = (
            base64.urlsafe_b64decode(cursor).decode("ascii").split(":")
        )
        offset = int(offset_text)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise PermanentSourceError("cursor is not one this source issued") from None
    if label != "snapshot" or not _SNAPSHOT_ID.fullmatch(snapshot_id) or offset < 0:
        raise PermanentSourceError("cursor is not one this source issued")
    return snapshot_id, offset


async def _read_capped(response: httpx.Response, what: str) -> bytes:
    declared = response.headers.get("Content-Length", "")
    if declared.isdecimal() and int(declared) > MAX_RESPONSE_BYTES:
        raise PermanentSourceError(f"{what}: the response is larger than this adapter will read")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise PermanentSourceError(
                f"{what}: the response is larger than this adapter will read"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _payload_bytes(items: list[Any]) -> int:
    return sum(
        len((item.record if isinstance(item, SourcePatient) else item).payload) for item in items
    )


def _checked(response: httpx.Response, what: str) -> httpx.Response:
    status = response.status_code
    if response.is_success:
        return response
    if status == httpx.codes.NOT_FOUND:
        raise RecordNotFoundError(f"{what} not found")
    if status == httpx.codes.TOO_MANY_REQUESTS:
        message = f"{what}: the source asked the client to slow down"
        raise RateLimitedError(message, _retry_after(response))
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise RetryableSourceError(f"{what}: source error ({status})")
    raise PermanentSourceError(f"{what}: the source refused the request ({status})")


def _resources_of(bundle: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    """The resources of a complete searchset of one type, or a typed error.

    A server that pages (HAPI does, by default) would otherwise be read as a short result and
    its records silently dropped, so a ``next`` link or a ``total`` that disagrees with the
    entries is refused.
    """
    if bundle.get("resourceType") != "Bundle":
        raise PermanentSourceError(f"{resource_type}: the source did not return a bundle")
    links, entries = bundle.get("link", []), bundle.get("entry", [])
    if not isinstance(links, list) or not isinstance(entries, list):
        raise PermanentSourceError(f"{resource_type}: the source returned a malformed bundle")
    if any(isinstance(link, dict) and link.get("relation") == "next" for link in links):
        raise PermanentSourceError(f"{resource_type}: the source pages its results")
    resources = [_resource_of(entry, resource_type) for entry in entries if not _is_outcome(entry)]
    total = bundle.get("total")
    if total is not None and not (str(total).isdecimal() and int(str(total)) == len(resources)):
        problem = "malformed" if not str(total).isdecimal() else "incomplete"
        raise PermanentSourceError(f"{resource_type}: the source returned a {problem} bundle")
    return resources


def _is_outcome(entry: object) -> bool:
    """An OperationOutcome entry (``search.mode`` outcome) reports on the search, not a match."""
    search = entry.get("search") if isinstance(entry, dict) else None
    return isinstance(search, dict) and search.get("mode") == "outcome"


def _resource_of(entry: object, resource_type: str) -> dict[str, Any]:
    resource = entry.get("resource") if isinstance(entry, dict) else None
    if (
        not isinstance(resource, dict)
        or resource.get("resourceType") != resource_type
        or not isinstance(resource.get("id"), str)
    ):
        raise PermanentSourceError(f"{resource_type}: the source returned a malformed bundle")
    return resource


def _retry_after(response: httpx.Response) -> float | None:
    """The server's retry hint in seconds, kept finite and bounded so a caller can honor it."""
    try:
        seconds = float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), MAX_RETRY_AFTER_SECONDS)


def _require_resource_type(resource_type: str) -> None:
    """Only the types this adapter reads are ever requested, which also keeps paths fixed."""
    if resource_type not in READ_RESOURCE_TYPES:
        raise RecordNotFoundError("the adapter does not read that resource type")


def _require_fhir_id(resource_type: str, resource_id: str) -> None:
    """Refuse values that could change the request path; no such resource can exist."""
    if not _FHIR_ID.fullmatch(resource_id):
        raise RecordNotFoundError(f"{resource_type}: not a FHIR id")


def _parse_json(content: bytes) -> dict[str, Any]:
    try:
        document = json.loads(content, parse_float=_Number, parse_int=_Number)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PermanentSourceError("the source returned a response that is not JSON") from None
    if not isinstance(document, dict):
        raise PermanentSourceError("the source returned an unexpected response")
    return document


def _meta(resource: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = resource.get("meta")
    return meta if isinstance(meta, dict) else {}


def _instant(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise PermanentSourceError("the source sent a lastUpdated that is no timestamp") from None
    if parsed.utcoffset() is None:
        raise PermanentSourceError("the source sent a lastUpdated without a timezone")
    return parsed


def _dump(value: Any) -> str:
    """Compact JSON that keeps key order and every number token as received."""
    match value:
        case dict():
            members = (
                f"{json.dumps(key, ensure_ascii=False)}:{_dump(item)}"
                for key, item in value.items()
            )
            return "{" + ",".join(members) + "}"
        case list():
            return "[" + ",".join(_dump(item) for item in value) + "]"
        case _Number():
            return str(value)
        case str() | bool() | None:
            return json.dumps(value, ensure_ascii=False)
    raise PermanentSourceError(f"unexpected JSON value of type {type(value).__name__}")
