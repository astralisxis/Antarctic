"""store the paying user for gifted orders

Revision ID: c84e3a91d762
Revises: a61f3d904c27
Create Date: 2026-08-23 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84e3a91d762"
down_revision: str | None = "a61f3d904c27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("buyer_user_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_orders_buyer_user_id", ["buyer_user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_orders_buyer_user_id_users",
            "users",
            ["buyer_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute("UPDATE orders SET buyer_user_id = user_id WHERE buyer_user_id IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_constraint("fk_orders_buyer_user_id_users", type_="foreignkey")
        batch_op.drop_index("ix_orders_buyer_user_id")
        batch_op.drop_column("buyer_user_id")
