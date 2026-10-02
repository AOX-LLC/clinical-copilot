"""A tombstone on the source resource head.

A full sync that read a patient's records to the end can tell that a record it used to see
is gone from the source. Snapshots are immutable, so the fact lives on the head: ``deleted_at``
is when a sync first noticed the record missing, and it is cleared when the record is seen
again. The app role already holds UPDATE on the head (0001), so no grant changes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_resource_head",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Dropping the column would turn every tombstoned record back into a current one while
    # its timeline rows stay superseded. Refuse rather than leave the two disagreeing.
    bind = op.get_bind()
    remaining = bind.exec_driver_sql(
        "SELECT count(*) FROM source_resource_head WHERE deleted_at IS NOT NULL"
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} tombstoned source resources exist; see them back or clear "
            "deleted_at before downgrading"
        )
    op.drop_column("source_resource_head", "deleted_at")
