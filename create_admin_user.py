"""Crea un usuario o restablece su contraseña (desde la consola del servidor).

Uso, desde la raíz del proyecto (con la BD ya migrada: `alembic upgrade head`):

    # Tu usuario de plataforma (dueño de LaborLog): crea y administra empresas
    python -m laborlog_backend.create_admin_user --rol plataforma --usuario nelson

    # Un usuario de una empresa existente (por su código)
    python -m laborlog_backend.create_admin_user --empresa empresa-principal --usuario admin --rol superadmin

    # Crear la empresa si no existe
    python -m laborlog_backend.create_admin_user --empresa constructora-abc --nombre-empresa "Constructora ABC" \\
        --usuario dueno --rol superadmin

La contraseña se pide por consola (no queda en el historial). Si el usuario ya existe
en esa empresa, se actualiza su contraseña y rol, y se reactiva.

En Docker:
    docker exec -it laborlog-backend python -m laborlog_backend.create_admin_user ...
"""
import argparse
import getpass
import re
import sys
import uuid
from datetime import datetime

from sqlalchemy import func

from laborlog_backend.auth import CODIGO_PLATAFORMA, ROL_PLATAFORMA, ROLES_VALIDOS, get_password_hash
from laborlog_backend.database import SessionLocal
from laborlog_backend.models import Empresa, UsuarioAdmin

PATRON_USUARIO = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usuario", required=True, help="nombre de usuario para iniciar sesión")
    parser.add_argument("--email", default=None)
    parser.add_argument("--nombre", default=None, help="nombre de la persona")
    parser.add_argument("--rol", default="superadmin", choices=sorted(ROLES_VALIDOS))
    parser.add_argument("--empresa", default=None, help="código de la empresa (no aplica al rol plataforma)")
    parser.add_argument("--nombre-empresa", default=None, help="si la empresa no existe, se crea con este nombre")
    args = parser.parse_args()

    usuario = args.usuario.strip().lower()
    if not PATRON_USUARIO.match(usuario):
        print("❌ Usuario inválido: 3 a 40 caracteres, minúsculas, números, punto, guion o guion bajo.")
        return 1
    email = (args.email or "").strip().lower() or None

    db = SessionLocal()
    try:
        empresa = None
        if args.rol == ROL_PLATAFORMA:
            if args.empresa:
                print("❌ El usuario de plataforma no pertenece a ninguna empresa (quita --empresa).")
                return 1
        else:
            codigo = (args.empresa or "").strip().lower()
            if not codigo or codigo == CODIGO_PLATAFORMA:
                print("❌ Indica el código de la empresa con --empresa.")
                return 1
            empresa = db.query(Empresa).filter(func.lower(Empresa.slug) == codigo).first()
            if empresa is None:
                if not args.nombre_empresa:
                    print(f"❌ No existe la empresa '{codigo}'. Usa --nombre-empresa para crearla.")
                    return 1
                empresa = Empresa(id=uuid.uuid4(), nombre=args.nombre_empresa, slug=codigo, activa=True,
                                  created_at=datetime.now())
                db.add(empresa)
                db.flush()
                print(f"🏢 Empresa creada: {empresa.nombre} (código {codigo})")

        password = getpass.getpass("Contraseña (mínimo 8 caracteres): ")
        if len(password) < 8 or password != getpass.getpass("Repite la contraseña: "):
            print("❌ Las contraseñas no coinciden o son muy cortas.")
            return 1

        q = db.query(UsuarioAdmin).filter(func.lower(UsuarioAdmin.usuario) == usuario)
        q = q.filter(UsuarioAdmin.empresa_id.is_(None)) if empresa is None else q.filter(UsuarioAdmin.empresa_id == empresa.id)
        user = q.first()
        if user:
            user.hash_password = get_password_hash(password)
            user.rol = args.rol
            user.activo = True
            if email:
                user.email = email
            print(f"🔑 Usuario actualizado: {usuario} (rol {args.rol})")
        else:
            db.add(UsuarioAdmin(
                id=uuid.uuid4(),
                nombre=args.nombre or usuario,
                usuario=usuario,
                email=email,
                hash_password=get_password_hash(password),
                rol=args.rol,
                activo=True,
                empresa_id=empresa.id if empresa else None,
            ))
            donde = f"empresa {empresa.slug}" if empresa else "plataforma"
            print(f"🎉 Usuario creado: {usuario} (rol {args.rol}) en {donde}")
        db.commit()
        codigo_login = empresa.slug if empresa else CODIGO_PLATAFORMA
        print(f"   Inicia sesión con empresa '{codigo_login}' y usuario '{usuario}'.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
