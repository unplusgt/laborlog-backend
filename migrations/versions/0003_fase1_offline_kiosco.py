"""Fase 1: modo kiosco (obra por dispositivo) y marcajes offline

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dispositivos", sa.Column("obra_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_dispositivos_obra_id", "dispositivos", "obras", ["obra_id"], ["id"]
    )

    op.add_column("marcajes", sa.Column("precision_metros", sa.Float(), nullable=True))
    op.add_column(
        "marcajes",
        sa.Column("capturado_offline", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("marcajes", "capturado_offline")
    op.drop_column("marcajes", "precision_metros")
    op.drop_constraint("fk_dispositivos_obra_id", "dispositivos", type_="foreignkey")
    op.drop_column("dispositivos", "obra_id")
