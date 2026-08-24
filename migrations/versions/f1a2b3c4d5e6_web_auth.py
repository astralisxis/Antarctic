"""add web authentication identity fields

Revision ID: f1a2b3c4d5e6
Revises: e6b7c8d9a012
Create Date: 2026-08-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e6b7c8d9a012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("auth_provider", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("auth_subject", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("email", sa.String(length=255), nullable=True))
        batch_op.create_index("ix_users_auth_provider", ["auth_provider"], unique=False)
        batch_op.create_index("ix_users_auth_subject", ["auth_subject"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_auth_subject")
        batch_op.drop_index("ix_users_auth_provider")
        batch_op.drop_column("email")
        batch_op.drop_column("auth_subject")
        batch_op.drop_column("auth_provider")
