"""review text optional

Отзыв без текста — обычное дело: оценку человек ставит, писать не обязан.
Колонка стояла NOT NULL, и «без текста» падало на вставке.

Revision ID: d41a6f0b7c15
Revises: c2308e947720
Create Date: 2026-08-20 23:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd41a6f0b7c15'
down_revision: str | None = 'c2308e947720'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch — sqlite не умеет ALTER COLUMN, таблица пересоздаётся.
    with op.batch_alter_table('reviews') as batch:
        batch.alter_column('text', existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # Обратно нужен непустой текст: пустые отзывы подменяем прочерком.
    op.execute("UPDATE reviews SET text = '—' WHERE text IS NULL")
    with op.batch_alter_table('reviews') as batch:
        batch.alter_column('text', existing_type=sa.Text(), nullable=False)
