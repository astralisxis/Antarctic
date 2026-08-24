"""order guarantee and transfer metadata

Revision ID: a61f3d904c27
Revises: f28c6d49a102
Create Date: 2026-08-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a61f3d904c27"
down_revision: str | None = "f28c6d49a102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("country_offers", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("guarantee_hours", sa.Integer(), nullable=False, server_default="12")
        )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("guarantee_hours", sa.Integer(), nullable=False, server_default="12")
        )
        batch_op.add_column(sa.Column("replacement_requested_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("transferred_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("transfer_token", sa.String(length=64)))
        batch_op.create_index("ix_orders_transfer_token", ["transfer_token"], unique=True)
        batch_op.add_column(sa.Column("transfer_expires_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("transferred_at")
        batch_op.drop_column("replacement_requested_at")
        batch_op.drop_index("ix_orders_transfer_token")
        batch_op.drop_column("transfer_expires_at")
        batch_op.drop_column("transfer_token")
        batch_op.drop_column("guarantee_hours")

    with op.batch_alter_table("country_offers", schema=None) as batch_op:
        batch_op.drop_column("guarantee_hours")
