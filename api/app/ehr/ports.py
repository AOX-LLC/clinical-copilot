"""The EHR adapter interface every source system implements.

An adapter does transport and identity only: it fetches source records exactly as the
source returns them and says which resource and version each one is. Turning records
into timeline rows is the normalizers' job, so adapters stay small and the contract
suite in ``tests/contracts`` can hold every adapter to the same behavior.

Nothing in this module may put payload content into an exception message, a log line
or a ``repr``: payloads are patient records.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.timeline.canonical import content_sha256
from app.timeline.vocabulary import SourceKind

SHA256_LENGTH = 32


class RecordKind(StrEnum):
    """Source-agnostic categories an adapter can fetch; each adapter maps them to its types."""

    PATIENT = "patient"
    ENCOUNTER = "encounter"
    CONDITION = "condition"
    OBSERVATION = "observation"
    MEDICATION = "medication"
    CARE_PLAN = "care_plan"
    PROCEDURE = "procedure"
    IMMUNIZATION = "immunization"
    ALLERGY = "allergy"
    DOCUMENT = "document"
    APPOINTMENT = "appointment"


@dataclass(frozen=True, slots=True)
class SourceSystemRef:
    code: str
    kind: SourceKind


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One version of one source resource, exactly as the source returned it."""

    system: SourceSystemRef
    resource_type: str
    resource_id: str
    version_id: str | None
    source_updated_at: datetime | None
    payload: bytes = field(repr=False)
    content_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.resource_type or not self.resource_id:
            raise ValueError("a source record needs a resource type and id")
        if self.source_updated_at is not None and self.source_updated_at.utcoffset() is None:
            raise ValueError("source_updated_at must be timezone-aware")
        if len(self.content_sha256) != SHA256_LENGTH:
            raise ValueError("content_sha256 must be a 32-byte SHA-256 digest")

    @classmethod
    def from_payload(
        cls,
        system: SourceSystemRef,
        resource_type: str,
        resource_id: str,
        version_id: str | None,
        source_updated_at: datetime | None,
        payload: bytes,
    ) -> "SourceRecord":
        return cls(
            system=system,
            resource_type=resource_type,
            resource_id=resource_id,
            version_id=version_id,
            source_updated_at=source_updated_at,
            payload=payload,
            content_sha256=content_sha256(payload, system.kind),
        )


@dataclass(frozen=True, slots=True)
class SourcePatient:
    external_id: str
    record: SourceRecord


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    record_kinds: frozenset[RecordKind]
    supports_versions: bool
    supports_write_back: bool
    supports_notifications: bool


@dataclass(frozen=True, slots=True)
class ChangeNotification:
    """A source's signal that a resource changed. It carries ids only; the adapter re-fetches."""

    resource_type: str
    resource_id: str
    event_type: str


@dataclass(frozen=True, slots=True)
class ApprovedSummaryDocument:
    """A physician-approved summary revision, ready to write back to the source.

    The summary workflow builds this only from an approved revision; the revision hash
    binds what is written to exactly what the physician approved.
    """

    summary_id: UUID
    revision_sha256: bytes = field(repr=False)
    patient_external_id: str = field(repr=False)
    approved_by: UUID
    approved_at: datetime
    body: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WriteBackReceipt:
    resource_type: str
    resource_id: str
    version_id: str | None


class EhrAdapterError(Exception):
    """Base for adapter failures. Messages carry ids and reasons, never payload content."""


class RecordNotFoundError(EhrAdapterError):
    pass


class OperationNotSupportedError(EhrAdapterError):
    pass


class RateLimitedError(EhrAdapterError):
    def __init__(self, message: str, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RetryableSourceError(EhrAdapterError):
    pass


class PermanentSourceError(EhrAdapterError):
    pass


class SignatureInvalidError(EhrAdapterError):
    pass


class EhrAdapter(Protocol):
    @property
    def source(self) -> SourceSystemRef: ...

    async def capabilities(self) -> AdapterCapabilities: ...

    async def list_patients(self, cursor: str | None, page_size: int) -> Page[SourcePatient]: ...

    async def fetch_changes(
        self,
        patient_external_id: str,
        kinds: frozenset[RecordKind],
        since: datetime | None,
        cursor: str | None,
    ) -> Page[SourceRecord]:
        """Return records of the given kinds updated strictly after ``since``."""
        ...

    async def get_record(
        self, resource_type: str, resource_id: str, version_id: str | None = None
    ) -> SourceRecord:
        """Fetch one record, or one exact version of it, to re-resolve a citation."""
        ...

    async def write_back(
        self, document: ApprovedSummaryDocument, idempotency_key: str
    ) -> WriteBackReceipt:
        """Write an approved summary; the same key twice yields one target record."""
        ...

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> list[ChangeNotification]:
        """Verify a signed change notification and return the changes it names."""
        ...
