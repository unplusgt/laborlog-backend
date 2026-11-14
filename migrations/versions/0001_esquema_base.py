"""Esquema base (multi-empresa) - idempotente

Reemplaza a create_all() + bootstrap_multi_tenant_defaults() que corrían al arrancar.
Es idempotente para funcionar en los tres casos que existen hoy:
  1. BD vacía (instalación nueva)
  2. BD restaurada desde backup_laborlog_20260413.sql (sin tabla empresas)
  3. BD actual que ya pasó por el bootstrap multi-empresa

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""
import uuid

from alembic import context, op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLAS_CON_EMPRESA = ["usuarios_admin", "trabajadores", "obras", "dispositivos", "marcajes"]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS empresas (
            id UUID PRIMARY KEY,
            nombre VARCHAR NOT NULL,
            slug VARCHAR UNIQUE NOT NULL,
            activa BOOLEAN DEFAULT TRUE,
            hora_extra_desde INTEGER DEFAULT 8,
            sobretiempo_factor FLOAT DEFAULT 1.5,
            domingo_factor FLOAT DEFAULT 2.0,
            festivo_factor FLOAT DEFAULT 2.0,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS obras (
            id SERIAL PRIMARY KEY,
            nombre VARCHAR NOT NULL,
            lat VARCHAR,
            lng VARCHAR,
            radio_metros INTEGER
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dispositivos (
            id UUID PRIMARY KEY,
            nombre VARCHAR,
            token_fcm VARCHAR,
            last_sync TIMESTAMP,
            licencia VARCHAR
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trabajadores (
            id UUID PRIMARY KEY,
            nombre VARCHAR NOT NULL,
            dni VARCHAR,
            telefono VARCHAR,
            codigo_empresa VARCHAR,
            fecha_registro TIMESTAMP,
            embedding BYTEA,
            foto_registro_path VARCHAR,
            activo BOOLEAN DEFAULT TRUE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usuarios_admin (
            id UUID PRIMARY KEY,
            nombre VARCHAR NOT NULL,
            email VARCHAR UNIQUE NOT NULL,
            hash_password VARCHAR NOT NULL,
            rol VARCHAR
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS marcajes (
            id UUID PRIMARY KEY,
            trabajador_id UUID REFERENCES trabajadores(id),
            obra_id INTEGER REFERENCES obras(id),
            tipo VARCHAR,
            timestamp_server TIMESTAMP,
            timestamp_local TIMESTAMP NOT NULL,
            origen UUID REFERENCES dispositivos(id)
        )
        """
    )
    # Columnas que se agregaron a marcajes después de la primera versión
    for col in ["lat", "lng", "foto_path", "estado_sync", "razon_conflicto"]:
        op.execute(f"ALTER TABLE marcajes ADD COLUMN IF NOT EXISTS {col} VARCHAR")

    # Multi-empresa: columna empresa_id + índice en cada tabla
    for tabla in TABLAS_CON_EMPRESA:
        op.execute(
            f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS empresa_id UUID REFERENCES empresas(id)"
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{tabla}_empresa_id ON {tabla} (empresa_id)")

    # Empresa por defecto para datos existentes sin empresa (solo aplica contra una BD real)
    if context.is_offline_mode():
        return
    conn = op.get_bind()
    empresa_id = conn.execute(
        sa.text("SELECT id FROM empresas ORDER BY created_at ASC LIMIT 1")
    ).scalar()
    if not empresa_id:
        hay_datos = any(
            conn.execute(sa.text(f"SELECT 1 FROM {t} WHERE empresa_id IS NULL LIMIT 1")).first()
            for t in TABLAS_CON_EMPRESA
        )
        if hay_datos:
            empresa_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO empresas (id, nombre, slug) VALUES (:id, :nombre, :slug)"
                ),
                {"id": empresa_id, "nombre": "Empresa Principal", "slug": "empresa-principal"},
            )
    if empresa_id:
        for tabla in TABLAS_CON_EMPRESA:
            conn.execute(
                sa.text(f"UPDATE {tabla} SET empresa_id = :empresa_id WHERE empresa_id IS NULL"),
                {"empresa_id": empresa_id},
            )


def downgrade() -> None:
    # El esquema base no se revierte (borraría todos los datos).
    raise NotImplementedError("No se puede revertir el esquema base")
