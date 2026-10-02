"""A timeline kind for care plans.

``protocol`` is reserved for the practice's supplement protocols, so an EHR care plan
gets its own kind. Adding an enum value needs no table change and no grant.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_BEFORE = (
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
)


def upgrade() -> None:
    op.execute("ALTER TYPE timeline_kind ADD VALUE IF NOT EXISTS 'care_plan'")


def downgrade() -> None:
    # Postgres cannot drop one value from an enum, so the type is replaced. A care plan
    # row has no older kind to fall back to; refuse rather than rewrite or delete it.
    bind = op.get_bind()
    remaining = bind.exec_driver_sql(
        "SELECT count(*) FROM timeline_event WHERE kind = 'care_plan'"
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} care_plan timeline rows exist; remove them before downgrading"
        )
    quoted = ", ".join(f"'{kind}'" for kind in KINDS_BEFORE)
    op.execute("ALTER TYPE timeline_kind RENAME TO timeline_kind_old")
    op.execute(f"CREATE TYPE timeline_kind AS ENUM ({quoted})")
    op.execute(
        "ALTER TABLE timeline_event ALTER COLUMN kind TYPE timeline_kind"
        " USING kind::text::timeline_kind"
    )
    op.execute("DROP TYPE timeline_kind_old")
