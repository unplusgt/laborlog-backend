"""Fase 2: reconocimiento facial (huella, consentimiento y datos del marcaje)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trabajadores", sa.Column("embedding_modelo", sa.String(), nullable=True))
    op.add_column("trabajadores", sa.Column("rostro_registrado_en", sa.TIMESTAMP(), nullable=True))
    op.add_column("trabajadores", sa.Column("consentimiento_biometrico_en", sa.TIMESTAMP(), nullable=True))

    op.add_column("marcajes", sa.Column("metodo", sa.String(), nullable=True))
    op.add_column("marcajes", sa.Column("similitud", sa.Float(), nullable=True))
    op.add_column("marcajes", sa.Column("similitud_servidor", sa.Float(), nullable=True))
    op.add_column("marcajes", sa.Column("prueba_vida", sa.String(), nullable=True))


def downgrade() -> None:
    for col in ("prueba_vida", "similitud_servidor", "similitud", "metodo"):
        op.drop_column("marcajes", col)
    for col in ("consentimiento_biometrico_en", "rostro_registrado_en", "embedding_modelo"):
        op.drop_column("trabajadores", col)
