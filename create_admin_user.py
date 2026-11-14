"""Crea (o actualiza la contraseña de) un usuario administrador.

Uso, desde la raíz del proyecto (con la BD ya migrada: `alembic upgrade head`):

    python -m laborlog_backend.create_admin_user --email admin@miempresa.com --nombre "Admin"

La contraseña se pide por consola (no queda en el historial). Opciones:
    --rol admin|supervisor|superadmin   (por defecto: admin)
    --empresa "Nombre"                  (crea la empresa si no existe ninguna)

En Docker:
    docker exec -it laborlog-backend python -m laborlog_backend.create_admin_user --email ...
"""
import argparse
import getpass
import re
import sys
import uuid

from laborlog_backend.auth import ROLES_VALIDOS, get_password_hash
from laborlog_backend.database import SessionLocal
from laborlog_backend.models import Empresa, UsuarioAdmin


def _slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-") or "empresa"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--nombre", default="Administrador")
    parser.add_argument("--rol", default="admin", choices=sorted(ROLES_VALIDOS))
    parser.add_argument("--empresa", default="Empresa Principal")
    args = parser.parse_args()

    password = getpass.getpass("Contraseña (mínimo 8 caracteres): ")
    if len(password) < 8 or password != getpass.getpass("Repite la contraseña: "):
        print("❌ Las contraseñas no coinciden o son muy cortas.")
        return 1

    email = args.email.strip().lower()
    db = SessionLocal()
    try:
        empresa = db.query(Empresa).order_by(Empresa.created_at).first()
        if not empresa:
            empresa = Empresa(id=uuid.uuid4(), nombre=args.empresa, slug=_slug(args.empresa))
            db.add(empresa)
            db.flush()
            print(f"🏢 Empresa creada: {empresa.nombre}")

        user = db.query(UsuarioAdmin).filter(UsuarioAdmin.email == email).first()
        if user:
            user.hash_password = get_password_hash(password)
            user.rol = args.rol
            print(f"🔑 Usuario existente actualizado: {email} (rol {args.rol})")
        else:
            db.add(
                UsuarioAdmin(
                    nombre=args.nombre,
                    email=email,
                    hash_password=get_password_hash(password),
                    rol=args.rol,
                    empresa_id=empresa.id,
                )
            )
            print(f"🎉 Usuario creado: {email} (rol {args.rol}) en {empresa.nombre}")
        db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
