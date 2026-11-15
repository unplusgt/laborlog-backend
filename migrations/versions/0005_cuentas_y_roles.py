"""Cuentas por empresa y roles (plataforma, superadmin, jefe, supervisor)

- Login con código de empresa + usuario o email: usuario/email únicos por empresa.
- El rol "admin" anterior pasa a "superadmin" (dueño/RRHH de la empresa).
- Trabajadores asignados a una obra; supervisores con obras asignadas.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""
import re
from collections import defaultdict

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("usuarios_admin", sa.Column("usuario", sa.String(), nullable=True))
    op.add_column(
        "usuarios_admin",
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # El email deja de ser único en todo el sistema (ahora es único por empresa)
    insp = sa.inspect(bind)
    for uc in insp.get_unique_constraints("usuarios_admin"):
        if uc["column_names"] == ["email"]:
            op.drop_constraint(uc["name"], "usuarios_admin", type_="unique")
    for ix in insp.get_indexes("usuarios_admin"):
        if ix["column_names"] == ["email"] and ix.get("unique"):
            op.drop_index(ix["name"], table_name="usuarios_admin")
    op.alter_column("usuarios_admin", "email", existing_type=sa.String(), nullable=True)

    # Nombre de usuario a partir del email (parte antes de la @), único por empresa
    usados = defaultdict(set)
    filas = bind.execute(sa.text("SELECT id, empresa_id, email FROM usuarios_admin ORDER BY email")).fetchall()
    for fila_id, empresa_id, email in filas:
        base = re.sub(r"[^a-z0-9._-]", "", (email or "usuario").split("@")[0].lower()) or "usuario"
        base = base.ljust(3, "0")[:40]
        nombre, n = base, 2
        while nombre in usados[empresa_id]:
            nombre = f"{base}{n}"
            n += 1
        usados[empresa_id].add(nombre)
        bind.execute(sa.text("UPDATE usuarios_admin SET usuario = :u WHERE id = :id"), {"u": nombre, "id": fila_id})
    op.alter_column("usuarios_admin", "usuario", existing_type=sa.String(), nullable=False)
    op.create_unique_constraint("uq_usuarios_empresa_usuario", "usuarios_admin", ["empresa_id", "usuario"])
    op.create_unique_constraint("uq_usuarios_empresa_email", "usuarios_admin", ["empresa_id", "email"])

    op.execute("UPDATE usuarios_admin SET rol = 'superadmin' WHERE lower(rol) = 'admin'")
    op.execute("UPDATE empresas SET slug = lower(slug)")

    op.add_column("trabajadores", sa.Column("obra_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_trabajadores_obra_id", "trabajadores", "obras", ["obra_id"], ["id"])

    op.create_table(
        "supervisor_obras",
        sa.Column(
            "usuario_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usuarios_admin.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("obra_id", sa.Integer(), sa.ForeignKey("obras.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("supervisor_obras")
    op.drop_constraint("fk_trabajadores_obra_id", "trabajadores", type_="foreignkey")
    op.drop_column("trabajadores", "obra_id")
    op.execute("UPDATE usuarios_admin SET rol = 'admin' WHERE rol IN ('superadmin', 'jefe')")
    op.drop_constraint("uq_usuarios_empresa_email", "usuarios_admin", type_="unique")
    op.drop_constraint("uq_usuarios_empresa_usuario", "usuarios_admin", type_="unique")
    op.drop_column("usuarios_admin", "activo")
    op.drop_column("usuarios_admin", "usuario")
