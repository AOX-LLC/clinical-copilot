"""Field encryption: wrapped data keys and blind indexes.

``data_key`` holds each patient's data key wrapped under a key-encryption key that lives
outside the database. The application role may read and insert it but never update it: a
key is destroyed by the owner role, which makes the patient's encrypted fields unreadable.
``patient_blind_index`` replaces the single ``patient.name_bidx`` column with one digest row
per name token, birth date and identifier, so exact-match lookup needs no stored name.

Downgrading drops both tables. Dropping ``data_key`` destroys every wrapped key, so any
data already sealed becomes unreadable: downgrade only a database holding no real sealed data.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "copilot_app"
APP_GRANTS = {
    "data_key": "SELECT, INSERT",
    "patient_blind_index": "SELECT, INSERT, DELETE",
}


def upgrade() -> None:
    op.drop_index("ix_patient_name_bidx", table_name="patient")
    op.drop_column("patient", "name_bidx")

    op.create_table(
        "data_key",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("kek_version", sa.SmallInteger(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_data_key"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], name="fk_data_key_patient_id_patient"
        ),
        sa.UniqueConstraint("patient_id", name="uq_data_key_patient_id"),
        sa.CheckConstraint(
            "(wrapped_key IS NULL) = (destroyed_at IS NOT NULL)",
            name=op.f("ck_data_key_key_present_unless_destroyed"),
        ),
    )
    op.create_index(
        "ux_data_key_one_system_key",
        "data_key",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("patient_id IS NULL"),
    )

    op.create_table(
        "patient_blind_index",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("digest", sa.LargeBinary(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("kind", "digest", "patient_id", name="pk_patient_blind_index"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patient.id"], name="fk_patient_blind_index_patient_id_patient"
        ),
        sa.CheckConstraint(
            "kind IN ('name_token', 'birth_date', 'identifier')",
            name=op.f("ck_patient_blind_index_kind_known_value"),
        ),
        sa.CheckConstraint(
            "octet_length(digest) = 32", name=op.f("ck_patient_blind_index_digest_is_sha256_length")
        ),
    )
    op.create_index("ix_patient_blind_index_patient_id", "patient_blind_index", ["patient_id"])

    for table, privileges in APP_GRANTS.items():
        op.execute(f"GRANT {privileges} ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("patient_blind_index")
    op.drop_table("data_key")
    op.add_column("patient", sa.Column("name_bidx", sa.LargeBinary(), nullable=True))
    op.create_index("ix_patient_name_bidx", "patient", ["name_bidx"])
