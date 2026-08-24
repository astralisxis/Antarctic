"""one balance transaction of each kind per order

Revision ID: f28c6d49a102
Revises: e5a91c3d7f24
Create Date: 2026-08-21 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f28c6d49a102"
down_revision: str | None = "e5a91c3d7f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL order_id остаётся допустимым многократно; ограничение защищает только
    # проводки, связанные с конкретным заказом (purchase/refund).
    with op.batch_alter_table("balance_tx", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_balance_tx_order_kind", ["order_id", "kind"]
        )


def downgrade() -> None:
    with op.batch_alter_table("balance_tx", schema=None) as batch_op:
        batch_op.drop_constraint("uq_balance_tx_order_kind", type_="unique")
