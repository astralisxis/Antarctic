"""store replacement decisions and account validity checks

Revision ID: e6b7c8d9a012
Revises: c84e3a91d762
Create Date: 2026-08-23 16:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b7c8d9a012"
down_revision: str | None = "c84e3a91d762"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("replacement_status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("replacement_decided_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("replacement_error", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("replacement_lzt_item_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("replacement_lzt_cost", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("replacement_previous_item_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("account_valid", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("account_checked_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("account_invalid_reason", sa.String(length=500), nullable=True))
        batch_op.create_index("ix_orders_replacement_status", ["replacement_status"], unique=False)
        batch_op.create_index("ix_orders_replacement_lzt_item_id", ["replacement_lzt_item_id"], unique=False)
        batch_op.create_index("ix_orders_account_valid", ["account_valid"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_account_valid")
        batch_op.drop_index("ix_orders_replacement_lzt_item_id")
        batch_op.drop_index("ix_orders_replacement_status")
        batch_op.drop_column("account_invalid_reason")
        batch_op.drop_column("account_checked_at")
        batch_op.drop_column("account_valid")
        batch_op.drop_column("replacement_error")
        batch_op.drop_column("replacement_previous_item_id")
        batch_op.drop_column("replacement_lzt_cost")
        batch_op.drop_column("replacement_lzt_item_id")
        batch_op.drop_column("replacement_decided_at")
        batch_op.drop_column("replacement_status")
