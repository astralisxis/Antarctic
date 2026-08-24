"""review test entries

Тестовый отзыв отправляется из админки: клиента и заказа у него нет, ник и товар
админ пишет руками. Значит user_id больше не обязателен, а для подписи нужны
свои колонки — из заказа брать нечего.

Revision ID: b7d2f0c94e18
Revises: d41a6f0b7c15
Create Date: 2026-08-21 12:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7d2f0c94e18'
down_revision: str | None = 'd41a6f0b7c15'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch — sqlite не умеет ALTER COLUMN, таблица пересоздаётся.
    with op.batch_alter_table('reviews') as batch:
        batch.alter_column('user_id', existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column('author_name', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('product_title', sa.String(length=120), nullable=True))


def downgrade() -> None:
    # Отзывы без клиента обратно не отобразить — это тестовые, их и убираем.
    op.execute("DELETE FROM reviews WHERE user_id IS NULL")
    with op.batch_alter_table('reviews') as batch:
        batch.drop_column('product_title')
        batch.drop_column('author_name')
        batch.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
