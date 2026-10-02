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
SNAPSHOT_SECONDS = 300.0
MAX_SNAPSHOTS = 4

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

_RESOURCE_TYPE = re.compile(r"[A-Za-z]{1,64}")
_FHIR_ID = re.compile(r"[A-Za-z0-9\-.]{1,64}")
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{32}")
_now = time.monotonic


@dataclass(slots=True)
class _Snapshot:
    key: tuple[object, ...]
    items: list[Any]
    taken_at: float


class _Number(str):
    """A JSON number kept as its source text, so ``1.50`` is not rewritten as ``1.5``."""

    __slots__ = ()


class FhirR4Adapter:
    def __init__(self, source: SourceSystemRef, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client
        self._snapshots: dict[str, _Snapshot] = {}

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
        response = await self._get(f"/{what}", {}, what)
        resource = _parse_json(response.content)
        current_version = _meta(resource).get("versionId")
        if version_id is not None and version_id != current_version:
            raise OperationNotSupportedError(
                f"{resource_type}/{resource_id}: the server cannot return version {version_id}"
            )
        return self._record_of(resource, payload=response.content)

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
        self._snapshots = {
            known_id: known
            for known_id, known in self._snapshots.items()
            if snapshot.taken_at - known.taken_at < SNAPSHOT_SECONDS
        }
        while len(self._snapshots) >= MAX_SNAPSHOTS:
            oldest = min(self._snapshots, key=lambda known_id: self._snapshots[known_id].taken_at)
            del self._snapshots[oldest]
        self._snapshots[snapshot_id] = snapshot

    def _recall(self, snapshot_id: str, key: tuple[object, ...]) -> _Snapshot:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None or _now() - snapshot.taken_at >= SNAPSHOT_SECONDS:
            self._snapshots.pop(snapshot_id, None)
            raise RetryableSourceError("the paging snapshot expired; start the listing again")
        if snapshot.key != key:
            raise PermanentSourceError("cursor belongs to a different listing")
        return snapshot

    async def _records_of_type(self, resource_type: str, patient_id: str) -> list[SourceRecord]:
        if resource_type == "Patient":
            return [await self.get_record("Patient", patient_id)]
        found = await self._search(resource_type, {"patient": patient_id})
        return [self._record_of(resource) for resource in found]

    async def _search(
        self, resource_type: str, parameters: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        # No _count: the server truncates without a next link, and returns everything without it.
        response = await self._get(f"/{resource_type}", parameters, resource_type)
        bundle = _parse_json(response.content)
        entries = bundle.get("entry", [])
        resources = [entry["resource"] for entry in entries]
        if not all(isinstance(r, dict) and "id" in r and "resourceType" in r for r in resources):
            raise PermanentSourceError(f"{resource_type}: the source returned a malformed bundle")
        return resources

    async def _get(self, path: str, parameters: Mapping[str, str], what: str) -> httpx.Response:
        try:
            response = await self._client.get(
                path, params=parameters, headers={"Accept": "application/fhir+json"}
            )
        except httpx.TransportError as error:
            reason = type(error).__name__
            raise RetryableSourceError(f"{what}: transport error ({reason})") from None
        return _checked(response, what)

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


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def _require_resource_type(resource_type: str) -> None:
    """Refuse values that could change the request path; no such resource can exist."""
    if not _RESOURCE_TYPE.fullmatch(resource_type):
        raise RecordNotFoundError("not a FHIR resource type")


def _require_fhir_id(resource_type: str, resource_id: str) -> None:
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
