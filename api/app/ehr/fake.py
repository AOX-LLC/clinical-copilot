"""An in-memory, FHIR-shaped source system.

It implements the full adapter interface so the contract suite and the ingestion tests
can run without a server. It also simulates the source behaviors provenance has to
survive: a new version of a resource, and a server that is wiped and reloaded with
identical content (version ids restart at 1, timestamps and ``meta.source`` change).
"""

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.ehr.ports import (
    AdapterCapabilities,
    ApprovedSummaryDocument,
    ChangeNotification,
    Page,
    PermanentSourceError,
    RateLimitedError,
    RecordKind,
    RecordNotFoundError,
    SignatureInvalidError,
    SourcePatient,
    SourceRecord,
    SourceSystemRef,
    WriteBackReceipt,
)

SIGNATURE_HEADER = "x-fake-signature"
KNOWN_EVENT_TYPES = frozenset({"created", "updated", "deleted"})
CHANGES_PAGE_SIZE = 2
_WRITE_BACK_NAMESPACE = uuid.UUID("6f1c2a0e-3b7d-4c55-9a51-0d6c8e1f2b47")

type ResourceKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _StoredVersion:
    version_id: str
    updated_at: datetime
    payload: bytes


class FakeAdapter:
    def __init__(
        self, source: SourceSystemRef, notification_secret: bytes, start: datetime
    ) -> None:
        if start.utcoffset() is None:
            raise ValueError("the fake source clock must be timezone-aware")
        self._source = source
        self._notification_secret = notification_secret
        self._clock = start
        self._load_count = 1
        self._versions: dict[ResourceKey, list[_StoredVersion]] = {}
        self._documents: dict[ResourceKey, dict[str, Any]] = {}
        self._kinds: dict[ResourceKey, RecordKind] = {}
        self._patient_of: dict[ResourceKey, str] = {}
        self._write_backs: dict[str, WriteBackReceipt] = {}
        self._throttle_next_call = False

    @property
    def source(self) -> SourceSystemRef:
        return self._source

    # Test controls: these simulate source behavior and are not part of the adapter interface.

    def put(self, kind: RecordKind, resource: Mapping[str, Any], patient_external_id: str) -> None:
        """Create or update a resource, assigning a new version like a FHIR server would."""
        key: ResourceKey = (resource["resourceType"], resource["id"])
        history = self._versions.setdefault(key, [])
        self._documents[key] = dict(resource)
        self._kinds[key] = kind
        self._patient_of[key] = patient_external_id
        history.append(self._stamp(key, version_number=len(history) + 1))

    def reset_and_reload(self) -> None:
        """Simulate wiping the server and reloading the same content.

        Every resource comes back with ``versionId`` 1, a new ``lastUpdated`` and a new
        ``meta.source``; the clinical content is unchanged.
        """
        self._load_count += 1
        for key in self._versions:
            self._versions[key] = [self._stamp(key, version_number=1)]

    def throttle_next_call(self) -> None:
        self._throttle_next_call = True

    def sign_notification(self, body: bytes) -> dict[str, str]:
        signature = hmac.new(self._notification_secret, body, hashlib.sha256).hexdigest()
        return {SIGNATURE_HEADER: signature}

    # Adapter interface.

    async def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            record_kinds=frozenset(RecordKind),
            supports_versions=True,
            supports_write_back=True,
            supports_notifications=True,
        )

    async def list_patients(self, cursor: str | None, page_size: int) -> Page[SourcePatient]:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        self._raise_if_throttled()
        patient_keys = sorted(
            key for key, kind in self._kinds.items() if kind is RecordKind.PATIENT
        )
        offset = _decode_cursor(cursor)
        page_keys = patient_keys[offset : offset + page_size]
        next_offset = offset + len(page_keys)
        return Page(
            items=tuple(SourcePatient(key[1], self._latest_record(key)) for key in page_keys),
            next_cursor=_encode_cursor(next_offset) if next_offset < len(patient_keys) else None,
        )

    async def fetch_changes(
        self,
        patient_external_id: str,
        kinds: frozenset[RecordKind],
        since: datetime | None,
        cursor: str | None,
    ) -> Page[SourceRecord]:
        self._raise_if_throttled()
        changed = sorted(
            (
                self._latest_record(key)
                for key, kind in self._kinds.items()
                if kind in kinds
                and self._patient_of[key] == patient_external_id
                and (since is None or self._versions[key][-1].updated_at > since)
            ),
            key=lambda record: (record.source_updated_at, record.resource_type, record.resource_id),
        )
        offset = _decode_cursor(cursor)
        page = changed[offset : offset + CHANGES_PAGE_SIZE]
        next_offset = offset + len(page)
        return Page(
            items=tuple(page),
            next_cursor=_encode_cursor(next_offset) if next_offset < len(changed) else None,
        )

    async def get_record(
        self, resource_type: str, resource_id: str, version_id: str | None = None
    ) -> SourceRecord:
        self._raise_if_throttled()
        key: ResourceKey = (resource_type, resource_id)
        history = self._versions.get(key)
        if history is None:
            raise RecordNotFoundError(f"{resource_type}/{resource_id} not found")
        if version_id is None:
            return self._as_record(key, history[-1])
        for stored in history:
            if stored.version_id == version_id:
                return self._as_record(key, stored)
        raise RecordNotFoundError(f"{resource_type}/{resource_id} has no version {version_id}")

    async def write_back(
        self, document: ApprovedSummaryDocument, idempotency_key: str
    ) -> WriteBackReceipt:
        self._raise_if_throttled()
        existing = self._write_backs.get(idempotency_key)
        if existing is not None:
            return existing

        resource_id = str(uuid.uuid5(_WRITE_BACK_NAMESPACE, idempotency_key))
        resource = {
            "resourceType": "DocumentReference",
            "id": resource_id,
            "status": "current",
            "subject": {"reference": f"Patient/{document.patient_external_id}"},
            "description": document.body,
        }
        self.put(RecordKind.DOCUMENT, resource, document.patient_external_id)
        receipt = WriteBackReceipt("DocumentReference", resource_id, "1")
        self._write_backs[idempotency_key] = receipt
        return receipt

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ChangeNotification]:
        lowered_headers = {name.lower(): value for name, value in headers.items()}
        provided = lowered_headers.get(SIGNATURE_HEADER, "").encode("utf-8", "replace")
        expected = self.sign_notification(body)[SIGNATURE_HEADER].encode("ascii")
        if not hmac.compare_digest(provided, expected):
            raise SignatureInvalidError("notification signature does not match")
        return _parse_events(body)

    def _stamp(self, key: ResourceKey, version_number: int) -> _StoredVersion:
        self._clock += timedelta(seconds=1)
        resource = dict(self._documents[key])
        resource["meta"] = {
            "versionId": str(version_number),
            "lastUpdated": self._clock.isoformat(),
            "source": f"#load-{self._load_count}",
        }
        payload = json.dumps(resource, indent=2).encode("utf-8")
        return _StoredVersion(str(version_number), self._clock, payload)

    def _latest_record(self, key: ResourceKey) -> SourceRecord:
        return self._as_record(key, self._versions[key][-1])

    def _as_record(self, key: ResourceKey, stored: _StoredVersion) -> SourceRecord:
        return SourceRecord.from_payload(
            system=self._source,
            resource_type=key[0],
            resource_id=key[1],
            version_id=stored.version_id,
            source_updated_at=stored.updated_at,
            payload=stored.payload,
        )

    def _raise_if_throttled(self) -> None:
        if self._throttle_next_call:
            self._throttle_next_call = False
            raise RateLimitedError("source asked the client to slow down", retry_after_seconds=1.0)


def _parse_events(body: bytes) -> list[ChangeNotification]:
    """Parse a verified notification body, ignoring event types this source does not know."""
    try:
        events = json.loads(body)["events"]
        if not isinstance(events, list):
            raise TypeError("events must be a list")
        return [_parse_event(event) for event in events if event["event_type"] in KNOWN_EVENT_TYPES]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise PermanentSourceError("notification body is not the expected shape") from error


def _parse_event(event: Mapping[str, Any]) -> ChangeNotification:
    resource_type, resource_id = event["resource_type"], event["resource_id"]
    if not isinstance(resource_type, str) or not isinstance(resource_id, str):
        raise TypeError("resource_type and resource_id must be strings")
    return ChangeNotification(resource_type, resource_id, event["event_type"])


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        label, _, offset_text = base64.urlsafe_b64decode(cursor).decode("ascii").partition(":")
        offset = int(offset_text)
        if label != "offset" or offset < 0:
            raise ValueError("not an offset cursor")
        return offset
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise PermanentSourceError("cursor is not one this source issued") from error
