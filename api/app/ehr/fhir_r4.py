"""The FHIR R4 adapter.

It does transport and identity only: it fetches resources from a FHIR R4 server and says
which resource and version each one is. Normalizers turn them into timeline rows.

What the server cannot do is declared, not faked (ADR 0013): it has no exact-version reads
(``supports_versions``), cannot answer "changed after" reliably (``supports_since``), and
does not page, so the adapter reads a whole result and hands out offset cursors over it.
Search results are re-serialized compactly with every number token and key kept, which
hashes the same as the bytes a direct read returns.

Nothing here puts payload content into an exception message, a log line or a ``repr``.
"""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from app.ehr.cursors import decode_offset_cursor, encode_offset_cursor
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
REQUEST_TIMEOUT_SECONDS = 60.0

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


class _Number(str):
    """A JSON number kept as its source text, so ``1.50`` is not rewritten as ``1.5``."""

    __slots__ = ()


class FhirR4Adapter:
    def __init__(self, source: SourceSystemRef, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client

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
        offset = decode_offset_cursor(cursor)
        patients = sorted(await self._search("Patient", {}), key=lambda item: item["id"])
        page = patients[offset : offset + page_size]
        next_offset = offset + len(page)
        return Page(
            items=tuple(SourcePatient(item["id"], self._record_of(item)) for item in page),
            next_cursor=encode_offset_cursor(next_offset) if next_offset < len(patients) else None,
        )

    async def fetch_changes(
        self,
        patient_external_id: str,
        kinds: frozenset[RecordKind],
        since: datetime | None,
        cursor: str | None,
    ) -> Page[SourceRecord]:
        """Return every record of the kinds for the patient; ``since`` is ignored (ADR 0013)."""
        offset = decode_offset_cursor(cursor)
        _require_fhir_id("Patient", patient_external_id)
        records: list[SourceRecord] = []
        for kind in sorted(kinds & RESOURCE_TYPES_BY_KIND.keys()):
            for resource_type in RESOURCE_TYPES_BY_KIND[kind]:
                records.extend(await self._records_of_type(resource_type, patient_external_id))
        records.sort(key=lambda record: (record.resource_type, record.resource_id))
        page = records[offset : offset + CHANGES_PAGE_SIZE]
        next_offset = offset + len(page)
        return Page(
            items=tuple(page),
            next_cursor=encode_offset_cursor(next_offset) if next_offset < len(records) else None,
        )

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
