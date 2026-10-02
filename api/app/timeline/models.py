"""The normalized timeline schema.

``source_record`` holds one immutable, hashed snapshot per distinct version of a source
resource; the app role may only insert into it. ``source_resource_head`` says which
snapshot is current. ``timeline_event`` is a projection of the current snapshots and
can be rebuilt from them at any time. Citations point at snapshots, so they keep
resolving to the exact content cited after re-imports and source edits.

Columns ending in ``_enc`` hold ciphertext only, sealed through ``PayloadSealer`` by the
AES-GCM ``FieldSealer`` in ``app.crypto`` (docs/adr/0014-field-encryption-implementation.md).
``data_key`` holds each patient's data key wrapped under a key-encryption key that lives
outside the database, and ``patient_blind_index`` holds the HMAC digests that make exact-match
patient lookup possible without a stored name.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.timeline.vocabulary import (
    ImportStatus,
    ImportTrigger,
    RecordState,
    SourceKind,
    TimelineKind,
    TimePrecision,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _pg_enum[E: StrEnum](enum_type: type[E], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Every stored instant is timezone-aware; naive timestamps never reach the database.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class SourceSystem(Base):
    __tablename__ = "source_system"

    id: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[SourceKind] = mapped_column(_pg_enum(SourceKind, "source_kind"))
    display_name: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)


class ImportRun(Base):
    __tablename__ = "import_run"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_system_id: Mapped[int] = mapped_column(ForeignKey("source_system.id"))
    trigger: Mapped[ImportTrigger] = mapped_column(_pg_enum(ImportTrigger, "import_trigger"))
    status: Mapped[ImportStatus] = mapped_column(
        _pg_enum(ImportStatus, "import_status"), server_default=ImportStatus.RUNNING.value
    )
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    records_seen: Mapped[int] = mapped_column(server_default=text("0"))
    snapshots_created: Mapped[int] = mapped_column(server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text)


class Patient(Base):
    __tablename__ = "patient"
    __table_args__ = (
        CheckConstraint(
            "sex_at_birth IN ('female', 'male', 'unknown')", name="sex_at_birth_known_value"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    given_name_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    family_name_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    birth_date_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    identifiers_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    sex_at_birth: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DataKey(Base):
    """A patient's data key, wrapped under the key-encryption key; one row with no patient is
    the system key for records with no patient subject.

    The app role may read and insert, never update: destroying a key (``wrapped_key`` to NULL,
    ``destroyed_at`` set) is an owner-role operation that makes the patient's data unreadable.
    """

    __tablename__ = "data_key"
    __table_args__ = (
        CheckConstraint(
            "(wrapped_key IS NULL) = (destroyed_at IS NOT NULL)",
            name="key_present_unless_destroyed",
        ),
        CheckConstraint(
            "wrapped_key IS NULL OR octet_length(wrapped_key) = 60",
            name="wrapped_key_has_wrapped_length",
        ),
        # NULL patients never collide under a plain unique constraint; this allows one system key.
        Index(
            "ux_data_key_one_system_key",
            text("(true)"),
            unique=True,
            postgresql_where=text("patient_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("patient.id"), unique=True)
    kek_version: Mapped[int] = mapped_column(SmallInteger)
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    destroyed_at: Mapped[datetime | None]


class PatientBlindIndex(Base):
    """HMAC digests of a patient's name tokens, birth date and identifiers, for exact lookup."""

    __tablename__ = "patient_blind_index"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('name_token', 'birth_date', 'identifier')", name="kind_known_value"
        ),
        CheckConstraint("octet_length(digest) = 32", name="digest_is_sha256_length"),
    )

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    digest: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patient.id"), primary_key=True, index=True
    )


class PatientSourceLink(Base):
    __tablename__ = "patient_source_link"

    source_system_id: Mapped[int] = mapped_column(ForeignKey("source_system.id"), primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patient.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StoredSourceRecord(Base):
    """One immutable snapshot of one distinct version of a source resource."""

    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint(
            "source_system_id",
            "resource_type",
            "resource_id",
            "content_sha256",
            name="uq_source_record_content",
        ),
        # Target of the head's composite foreign key: a head can only point at a
        # snapshot of the same resource.
        UniqueConstraint(
            "id",
            "source_system_id",
            "resource_type",
            "resource_id",
            name="uq_source_record_identity",
        ),
        CheckConstraint("octet_length(content_sha256) = 32", name="content_sha256_is_sha256"),
        CheckConstraint("resource_type <> '' AND resource_id <> ''", name="resource_named"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_system_id: Mapped[int] = mapped_column(ForeignKey("source_system.id"))
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[str] = mapped_column(Text)
    version_id: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None]
    content_sha256: Mapped[bytes] = mapped_column(LargeBinary)
    payload_enc: Mapped[bytes] = mapped_column(LargeBinary)
    state: Mapped[RecordState] = mapped_column(
        _pg_enum(RecordState, "record_state"), server_default=RecordState.PRESENT.value
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("patient.id"), index=True)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_run.id"))
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SourceResourceHead(Base):
    """Which snapshot of a source resource is current. Moved on every import."""

    __tablename__ = "source_resource_head"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_record_id", "source_system_id", "resource_type", "resource_id"],
            [
                "source_record.id",
                "source_record.source_system_id",
                "source_record.resource_type",
                "source_record.resource_id",
            ],
            name="fk_source_resource_head_snapshot",
        ),
    )

    source_system_id: Mapped[int] = mapped_column(ForeignKey("source_system.id"), primary_key=True)
    resource_type: Mapped[str] = mapped_column(Text, primary_key=True)
    resource_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_record_id: Mapped[uuid.UUID] = mapped_column(index=True)
    last_seen_at: Mapped[datetime]
    changed_at: Mapped[datetime]


class TimelineEvent(Base):
    """A normalized, queryable row projected from one current snapshot."""

    __tablename__ = "timeline_event"
    __table_args__ = (
        UniqueConstraint("source_record_id", "source_path"),
        CheckConstraint(
            "(time_precision = 'instant') = (occurred_at IS NOT NULL)",
            name="instant_has_occurred_at",
        ),
        CheckConstraint(
            "(time_precision IN ('day', 'month', 'year')) = (occurred_on IS NOT NULL)",
            name="calendar_precision_has_occurred_on",
        ),
        CheckConstraint(
            "(period_end_precision IS NULL"
            " AND period_end_at IS NULL AND period_end_on IS NULL)"
            " OR (period_end_precision = 'instant'"
            " AND period_end_at IS NOT NULL AND period_end_on IS NULL)"
            " OR (period_end_precision IN ('day', 'month', 'year')"
            " AND period_end_on IS NOT NULL AND period_end_at IS NULL)",
            name="period_end_matches_precision",
        ),
        CheckConstraint(
            "ref_low IS NULL OR ref_high IS NULL OR ref_low <= ref_high",
            name="reference_range_ordered",
        ),
        Index(
            "ix_timeline_event_current_by_patient",
            "patient_id",
            text("sort_at DESC"),
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_timeline_event_trend", "patient_id", "code_system", "code", "sort_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patient.id"))
    source_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_record.id"))
    source_path: Mapped[str] = mapped_column(Text, server_default="")
    kind: Mapped[TimelineKind] = mapped_column(_pg_enum(TimelineKind, "timeline_kind"))
    status: Mapped[str | None] = mapped_column(Text)
    code_system: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(Text)
    code_display: Mapped[str | None] = mapped_column(Text)

    time_precision: Mapped[TimePrecision] = mapped_column(_pg_enum(TimePrecision, "time_precision"))
    occurred_at: Mapped[datetime | None]
    occurred_on: Mapped[date | None]
    occurred_raw: Mapped[str | None] = mapped_column(Text)
    period_end_precision: Mapped[TimePrecision | None] = mapped_column(
        _pg_enum(TimePrecision, "time_precision")
    )
    period_end_at: Mapped[datetime | None]
    period_end_on: Mapped[date | None]
    sort_at: Mapped[datetime]
    recorded_at: Mapped[datetime | None]

    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric)
    value_unit: Mapped[str | None] = mapped_column(Text)
    value_text_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    ref_low: Mapped[Decimal | None] = mapped_column(Numeric)
    ref_high: Mapped[Decimal | None] = mapped_column(Numeric)
    ref_text: Mapped[str | None] = mapped_column(Text)
    source_interpretation: Mapped[str | None] = mapped_column(Text)
    detail_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    superseded_at: Mapped[datetime | None]
    projected_at: Mapped[datetime] = mapped_column(server_default=func.now())
