import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from . import models
from .database import get_db

load_dotenv()

# --- Configuración JWT -------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "")
if len(SECRET_KEY) < 32 or SECRET_KEY == "supersecretkey":
    raise RuntimeError(
        "SECRET_KEY falta o es insegura (mínimo 32 caracteres). Genera una con: "
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# --- Roles -------------------------------------------------------------------
# plataforma: dueño de LaborLog. Crea empresas; no ve datos internos de ninguna.
# superadmin: dueño / RRHH de la empresa. Todo, incluida la planilla.
# jefe:       todo menos la planilla (trabajadores, obras, dispositivos, supervisores).
# supervisor: solo las obras asignadas y los trabajadores de esas obras.
ROL_PLATAFORMA = "plataforma"
ROL_SUPERADMIN = "superadmin"
ROL_JEFE = "jefe"
ROL_SUPERVISOR = "supervisor"
ROLES_EMPRESA = {ROL_SUPERADMIN, ROL_JEFE, ROL_SUPERVISOR}
ROLES_VALIDOS = ROLES_EMPRESA | {ROL_PLATAFORMA}
ROLES_GESTION = {ROL_SUPERADMIN, ROL_JEFE}                    # catálogos, asignaciones, supervisores
ROLES_PLANILLA = {ROL_SUPERADMIN}                             # planilla y configuración de pago
ROLES_MARCAJE = {ROL_SUPERADMIN, ROL_JEFE, ROL_SUPERVISOR}    # pueden registrar marcajes
# Compatibilidad: "admin" era el administrador de la empresa antes de los roles nuevos
_ROLES_ANTIGUOS = {"admin": ROL_SUPERADMIN}
# Código de empresa reservado para entrar como usuario de plataforma
CODIGO_PLATAFORMA = "plataforma"

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")
oauth2_scheme_opcional = OAuth2PasswordBearer(tokenUrl="/login", auto_error=False)

DEVICE_TOKEN_HEADER = "X-Device-Token"


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def rol_de(usuario: models.UsuarioAdmin) -> str:
    rol = (usuario.rol or "").strip().lower()
    return _ROLES_ANTIGUOS.get(rol, rol)


def authenticate_user(db: Session, empresa_codigo: str, identificador: str, password: str):
    """Login con código de empresa + usuario o email + contraseña."""
    from sqlalchemy import func, or_

    codigo = (empresa_codigo or "").strip().lower()
    ident = (identificador or "").strip().lower()
    if not codigo or not ident:
        return None
    q = db.query(models.UsuarioAdmin).filter(
        or_(func.lower(models.UsuarioAdmin.usuario) == ident, func.lower(models.UsuarioAdmin.email) == ident)
    )
    if codigo == CODIGO_PLATAFORMA:
        q = q.filter(models.UsuarioAdmin.empresa_id.is_(None)).filter(models.UsuarioAdmin.rol == ROL_PLATAFORMA)
    else:
        empresa = db.query(models.Empresa).filter(func.lower(models.Empresa.slug) == codigo).first()
        if empresa is None or empresa.activa is False:
            return None
        q = q.filter(models.UsuarioAdmin.empresa_id == empresa.id)
    user = q.first()
    if not user or user.activo is False or not verify_password(password, user.hash_password):
        return None
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _usuario_desde_token(token: str, db: Session) -> models.UsuarioAdmin:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    try:
        user_uuid = uuid.UUID(str(user_id))
    except ValueError:
        raise credentials_exception
    user = db.query(models.UsuarioAdmin).filter(models.UsuarioAdmin.id == user_uuid).first()
    if user is None or user.activo is False:
        raise credentials_exception
    if user.empresa_id is not None:
        empresa = db.query(models.Empresa).filter(models.Empresa.id == user.empresa_id).first()
        if empresa is None or empresa.activa is False:
            raise HTTPException(status_code=403, detail="La empresa está desactivada")
    return user


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    return _usuario_desde_token(token, db)


def require_role(required_roles: Iterable[str]):
    """Dependencia que exige que el usuario tenga uno de los roles indicados."""
    roles = {r.lower() for r in required_roles}

    def role_checker(current_user: models.UsuarioAdmin = Depends(get_current_user)):
        if rol_de(current_user) not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No tienes permisos suficientes. Se requiere uno de: {sorted(roles)}",
            )
        return current_user

    return role_checker


# --- Tokens de dispositivo ---------------------------------------------------

def generar_token_dispositivo() -> str:
    return secrets.token_urlsafe(32)


def hash_token_dispositivo(token: str) -> str:
    # Los tokens son aleatorios de 256 bits: SHA-256 es suficiente y permite buscarlos por índice.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class ActorMarcaje:
    """Quién está registrando un marcaje: un usuario (supervisor/admin) o un dispositivo."""
    empresa_id: object
    usuario: Optional[models.UsuarioAdmin] = None
    dispositivo: Optional[models.Dispositivo] = None


def _dispositivo_desde_token(x_device_token: str, db: Session) -> models.Dispositivo:
    dispositivo = (
        db.query(models.Dispositivo)
        .filter(models.Dispositivo.token_hash == hash_token_dispositivo(x_device_token))
        .first()
    )
    if not dispositivo:
        raise HTTPException(status_code=401, detail="Token de dispositivo inválido")
    if not dispositivo.activo:
        raise HTTPException(status_code=403, detail="Dispositivo desactivado")
    empresa = db.query(models.Empresa).filter(models.Empresa.id == dispositivo.empresa_id).first()
    if empresa is None or empresa.activa is False:
        raise HTTPException(status_code=403, detail="La empresa está desactivada")
    return dispositivo


def get_dispositivo_actual(
    x_device_token: Optional[str] = Header(default=None, alias=DEVICE_TOKEN_HEADER),
    db: Session = Depends(get_db),
) -> models.Dispositivo:
    """Solo para endpoints del teléfono en modo kiosco (header X-Device-Token)."""
    if not x_device_token:
        raise HTTPException(status_code=401, detail=f"Falta el header {DEVICE_TOKEN_HEADER}")
    return _dispositivo_desde_token(x_device_token, db)


def get_actor_marcaje(
    token: Optional[str] = Depends(oauth2_scheme_opcional),
    x_device_token: Optional[str] = Header(default=None, alias=DEVICE_TOKEN_HEADER),
    db: Session = Depends(get_db),
) -> ActorMarcaje:
    if token:
        usuario = _usuario_desde_token(token, db)
        if rol_de(usuario) not in ROLES_MARCAJE:
            raise HTTPException(status_code=403, detail="Tu rol no puede registrar marcajes")
        if not usuario.empresa_id:
            raise HTTPException(status_code=403, detail="Usuario sin empresa asignada")
        return ActorMarcaje(empresa_id=usuario.empresa_id, usuario=usuario)

    if x_device_token:
        dispositivo = _dispositivo_desde_token(x_device_token, db)
        return ActorMarcaje(empresa_id=dispositivo.empresa_id, dispositivo=dispositivo)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Se requiere sesión de usuario o el header {DEVICE_TOKEN_HEADER}",
        headers={"WWW-Authenticate": "Bearer"},
    )
