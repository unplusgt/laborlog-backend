"""Fase 0: tokens de dispositivo, zona horaria por empresa y auditoría de marcajes

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "empresas",
        sa.Column("zona_horaria", sa.String(), nullable=False, server_default="America/Guatemala"),
    )

    op.add_column("dispositivos", sa.Column("token_hash", sa.String(), nullable=True))
    op.create_index("ix_dispositivos_token_hash", "dispositivos", ["token_hash"], unique=True)
    op.add_column(
        "dispositivos",
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.add_column(
        "marcajes",
        sa.Column(
            "registrado_por",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usuarios_admin.id", name="fk_marcajes_registrado_por"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_marcajes_registrado_por", "marcajes", type_="foreignkey")
    op.drop_column("marcajes", "registrado_por")
    op.drop_column("dispositivos", "activo")
    op.drop_index("ix_dispositivos_token_hash", table_name="dispositivos")
    op.drop_column("dispositivos", "token_hash")
    op.drop_column("empresas", "zona_horaria")
