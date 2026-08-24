"""admin ip bans

Счётчик неудачных входов по адресу и бан за перебор пароля. Таблица нужна в
базе, а не в памяти процесса: перезапуск админки не должен открывать подбор
заново, а список забаненных показывается на экране.

Revision ID: e5a91c3d7f24
Revises: b7d2f0c94e18
Create Date: 2026-08-21 15:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5a91c3d7f24'
down_revision: str | None = 'b7d2f0c94e18'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'admin_ip_bans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=False),
        sa.Column('fails', sa.Integer(), nullable=False),
        sa.Column('logins', sa.String(length=255), nullable=True),
        sa.Column('banned', sa.Boolean(), nullable=False),
        sa.Column('first_fail_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_fail_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('banned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('unbanned_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['unbanned_by'], ['admins.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_admin_ip_bans_ip', 'admin_ip_bans', ['ip'], unique=True)
    op.create_index('ix_admin_ip_bans_banned', 'admin_ip_bans', ['banned'])


def downgrade() -> None:
    op.drop_index('ix_admin_ip_bans_banned', table_name='admin_ip_bans')
    op.drop_index('ix_admin_ip_bans_ip', table_name='admin_ip_bans')
    op.drop_table('admin_ip_bans')
