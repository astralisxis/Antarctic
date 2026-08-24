"""add promo codes and redemptions

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-23 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "avatar_url" not in user_columns:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.add_column(sa.Column("avatar_url", sa.String(length=500), nullable=True))

    tables = set(inspector.get_table_names())
    if "promo_codes" not in tables:
        op.create_table(
            "promo_codes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=120), nullable=True),
            sa.Column("bonus", sa.Integer(), nullable=False),
            sa.Column("max_uses", sa.Integer(), nullable=True),
            sa.Column("uses_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["admins.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    existing = {index["name"] for index in sa.inspect(connection).get_indexes("promo_codes")}
    for name, columns, unique in (
        ("ix_promo_codes_code", ["code"], True),
        ("ix_promo_codes_is_active", ["is_active"], False),
        ("ix_promo_codes_expires_at", ["expires_at"], False),
        ("ix_promo_codes_created_at", ["created_at"], False),
    ):
        if name not in existing:
            op.create_index(name, "promo_codes", columns, unique=unique)

    if "promo_redemptions" not in tables:
        op.create_table(
            "promo_redemptions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("promo_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("bonus", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["promo_id"], ["promo_codes.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("promo_id", "user_id", name="uq_promo_redemption_user"),
        )
    existing = {index["name"] for index in sa.inspect(connection).get_indexes("promo_redemptions")}
    for name, columns in (
        ("ix_promo_redemptions_promo_id", ["promo_id"]),
        ("ix_promo_redemptions_user_id", ["user_id"]),
        ("ix_promo_redemptions_created_at", ["created_at"]),
    ):
        if name not in existing:
            op.create_index(name, "promo_redemptions", columns, unique=False)


def downgrade() -> None:
    op.drop_index("ix_promo_redemptions_created_at", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_user_id", table_name="promo_redemptions")
    op.drop_index("ix_promo_redemptions_promo_id", table_name="promo_redemptions")
    op.drop_table("promo_redemptions")
    op.drop_index("ix_promo_codes_created_at", table_name="promo_codes")
    op.drop_index("ix_promo_codes_expires_at", table_name="promo_codes")
    op.drop_index("ix_promo_codes_is_active", table_name="promo_codes")
    op.drop_index("ix_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("avatar_url")
