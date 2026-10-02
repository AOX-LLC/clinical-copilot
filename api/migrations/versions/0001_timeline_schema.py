"""Timeline schema: source snapshots, heads, patients and the normalized timeline.

Runs as the database owner. The application connects as ``copilot_app``, which gets
only the grants below: ``source_record`` is insert-only for it, so a stored snapshot can
never be changed or removed by application code.

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "copilot_app"
# Check constraint names below are wrapped in op.f(): they are already final, and the
# models' naming convention would otherwise prefix them a second time.

source_kind = postgresql.ENUM(
    "fhir_r4", "healthie", "lab_feed", name="source_kind", create_type=False
)
record_state = postgresql.ENUM("present", "deleted", name="record_state", create_type=False)
import_trigger = postgresql.ENUM(
    "manual", "schedule", "webhook", name="import_trigger", create_type=False
)
import_status = postgresql.ENUM(
    "running", "succeeded", "failed", name="import_status", create_type=False
)
timeline_kind = postgresql.ENUM(
    "encounter",
    "condition",
    "lab",
    "vital",
    "medication",
    "supplement",
    "protocol",
    "procedure",
    "immunization",
    "allergy",
    "note",
    "appointment",
    name="timeline_kind",
    create_type=False,
)
time_precision = postgresql.ENUM(
    "instant", "day", "month", "year", "unknown", name="time_precision", create_type=False
)
ENUMS = (source_kind, record_state, import_trigger, import_status, timeline_kind, time_precision)

TABLES_IN_CREATION_ORDER = (
    "source_system",
    "import_run",
    "patient",
    "patient_source_link",
    "source_record",
    "source_resource_head",
    "timeline_event",
)
APP_GRANTS = {
    "alembic_version": "SELECT",
    "source_system": "SELECT",
    "import_run": "SELECT, INSERT, UPDATE",
    "patient": "SELECT, INSERT, UPDATE",
    "patient_source_link": "SELECT, INSERT",
    "source_record": "SELECT, INSERT",
    "source_resource_head": "SELECT, INSERT, UPDATE",
    "timeline_event": "SELECT, INSERT, UPDATE",
}


def _timestamptz() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind)

    op.create_table(
        "source_system",
        sa.Column("id", sa.SmallInteger(), sa.Identity(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("kind", source_kind, nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_source_system"),
        sa.UniqueConstraint("code", name="uq_source_system_code"),
    )
    op.bulk_insert(
        sa.table(
            "source_system",
            sa.column("code", sa.Text()),
            sa.column("kind", source_kind),
            sa.column("display_name", sa.Text()),
        ),
        [
            {"code": "fhir-local", "kind": "fhir_r4", "display_name": "Local FHIR R4 server"},
            {"code": "healthie", "kind": "healthie", "display_name": "Healthie"},
            {"code": "lab-feed", "kind": "lab_feed", "display_name": "Simulated lab feed"},
        ],
    )

    op.create_table(
        "import_run",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_system_id", sa.SmallInteger(), nullable=False),
        sa.Column("trigger", import_trigger, nullable=False),
        sa.Column("status", import_status, server_default="running", nullable=False),
        sa.Column("started_at", _timestamptz(), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", _timestamptz(), nullable=True),
        sa.Column("records_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("snapshots_created", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_import_run"),
        sa.ForeignKeyConstraint(
            ["source_system_id"],
            ["source_system.id"],
            name="fk_import_run_source_system_id_source_system",
        ),
    )

    op.create_table(
        "patient",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("given_name_enc", sa.LargeBinary(), nullable=True),
        sa.Column("family_name_enc", sa.LargeBinary(), nullable=True),
        sa.Column("birth_date_enc", sa.LargeBinary(), nullable=True),
        sa.Column("identifiers_enc", sa.LargeBinary(), nullable=True),
        sa.Column("name_bidx", sa.LargeBinary(), nullable=True),
        sa.Column("sex_at_birth", sa.Text(), nullable=True),
        sa.Column("created_at", _timestamptz(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_patient"),
        sa.CheckConstraint(
            "sex_at_birth IN ('female', 'male', 'unknown')",
            name=op.f("ck_patient_sex_at_birth_known_value"),
        ),
    )
    op.create_index("ix_patient_name_bidx", "patient", ["name_bidx"])

    op.create_table(
        "patient_source_link",
        sa.Column("source_system_id", sa.SmallInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _timestamptz(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("source_system_id", "external_id", name="pk_patient_source_link"),
        sa.ForeignKeyConstraint(
            ["source_system_id"],
            ["source_system.id"],
            name="fk_patient_source_link_source_system_id_source_system",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], name="fk_patient_source_link_patient_id_patient"
        ),
    )
    op.create_index("ix_patient_source_link_patient_id", "patient_source_link", ["patient_id"])

    op.create_table(
        "source_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_system_id", sa.SmallInteger(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=True),
        sa.Column("source_updated_at", _timestamptz(), nullable=True),
        sa.Column("content_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("payload_enc", sa.LargeBinary(), nullable=False),
        sa.Column("state", record_state, server_default="present", nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("import_run_id", sa.Uuid(), nullable=True),
        sa.Column("fetched_at", _timestamptz(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_source_record"),
        sa.UniqueConstraint(
            "source_system_id",
            "resource_type",
            "resource_id",
            "content_sha256",
            name="uq_source_record_content",
        ),
        sa.UniqueConstraint(
            "id",
            "source_system_id",
            "resource_type",
            "resource_id",
            name="uq_source_record_identity",
        ),
        sa.CheckConstraint(
            "octet_length(content_sha256) = 32",
            name=op.f("ck_source_record_content_sha256_is_sha256"),
        ),
        sa.CheckConstraint(
            "resource_type <> '' AND resource_id <> ''",
            name=op.f("ck_source_record_resource_named"),
        ),
        sa.ForeignKeyConstraint(
            ["source_system_id"],
            ["source_system.id"],
            name="fk_source_record_source_system_id_source_system",
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], name="fk_source_record_patient_id_patient"
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"], ["import_run.id"], name="fk_source_record_import_run_id_import_run"
        ),
    )
    op.create_index("ix_source_record_patient_id", "source_record", ["patient_id"])

    op.create_table(
        "source_resource_head",
        sa.Column("source_system_id", sa.SmallInteger(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("last_seen_at", _timestamptz(), nullable=False),
        sa.Column("changed_at", _timestamptz(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source_system_id", "resource_type", "resource_id", name="pk_source_resource_head"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id", "source_system_id", "resource_type", "resource_id"],
            [
                "source_record.id",
                "source_record.source_system_id",
                "source_record.resource_type",
                "source_record.resource_id",
            ],
            name="fk_source_resource_head_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["source_system_id"],
            ["source_system.id"],
            name="fk_source_resource_head_source_system_id_source_system",
        ),
    )
    op.create_index(
        "ix_source_resource_head_source_record_id", "source_resource_head", ["source_record_id"]
    )

    op.create_table(
        "timeline_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("source_path", sa.Text(), server_default="", nullable=False),
        sa.Column("kind", timeline_kind, nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("code_system", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("code_display", sa.Text(), nullable=True),
        sa.Column("time_precision", time_precision, nullable=False),
        sa.Column("occurred_at", _timestamptz(), nullable=True),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("occurred_raw", sa.Text(), nullable=True),
        sa.Column("period_end_precision", time_precision, nullable=True),
        sa.Column("period_end_at", _timestamptz(), nullable=True),
        sa.Column("period_end_on", sa.Date(), nullable=True),
        sa.Column("sort_at", _timestamptz(), nullable=False),
        sa.Column("recorded_at", _timestamptz(), nullable=True),
        sa.Column("value_numeric", sa.Numeric(), nullable=True),
        sa.Column("value_unit", sa.Text(), nullable=True),
        sa.Column("value_text_enc", sa.LargeBinary(), nullable=True),
        sa.Column("ref_low", sa.Numeric(), nullable=True),
        sa.Column("ref_high", sa.Numeric(), nullable=True),
        sa.Column("ref_text", sa.Text(), nullable=True),
        sa.Column("source_interpretation", sa.Text(), nullable=True),
        sa.Column("detail_enc", sa.LargeBinary(), nullable=True),
        sa.Column("superseded_at", _timestamptz(), nullable=True),
        sa.Column("projected_at", _timestamptz(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_timeline_event"),
        sa.UniqueConstraint(
            "source_record_id", "source_path", name="uq_timeline_event_source_record_id_source_path"
        ),
        sa.CheckConstraint(
            "(time_precision = 'instant') = (occurred_at IS NOT NULL)",
            name=op.f("ck_timeline_event_instant_has_occurred_at"),
        ),
        sa.CheckConstraint(
            "(time_precision IN ('day', 'month', 'year')) = (occurred_on IS NOT NULL)",
            name=op.f("ck_timeline_event_calendar_precision_has_occurred_on"),
        ),
        sa.CheckConstraint(
            "(period_end_precision IS NULL"
            " AND period_end_at IS NULL AND period_end_on IS NULL)"
            " OR (period_end_precision = 'instant'"
            " AND period_end_at IS NOT NULL AND period_end_on IS NULL)"
            " OR (period_end_precision IN ('day', 'month', 'year')"
            " AND period_end_on IS NOT NULL AND period_end_at IS NULL)",
            name=op.f("ck_timeline_event_period_end_matches_precision"),
        ),
        sa.CheckConstraint(
            "ref_low IS NULL OR ref_high IS NULL OR ref_low <= ref_high",
            name=op.f("ck_timeline_event_reference_range_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], name="fk_timeline_event_patient_id_patient"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["source_record.id"],
            name="fk_timeline_event_source_record_id_source_record",
        ),
    )
    op.create_index(
        "ix_timeline_event_current_by_patient",
        "timeline_event",
        ["patient_id", sa.text("sort_at DESC")],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_timeline_event_trend",
        "timeline_event",
        ["patient_id", "code_system", "code", "sort_at"],
    )

    for table, privileges in APP_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in reversed(TABLES_IN_CREATION_ORDER):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind)
    op.execute("DROP EXTENSION IF EXISTS vector")
