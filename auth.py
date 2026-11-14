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
ROL_SUPERADMIN = "superadmin"
ROL_ADMIN = "admin"
ROL_SUPERVISOR = "supervisor"
ROLES_VALIDOS = {ROL_SUPERADMIN, ROL_ADMIN, ROL_SUPERVISOR}
ROLES_ADMIN = {ROL_SUPERADMIN, ROL_ADMIN}                     # gestión, planillas, configuración
ROLES_MARCAJE = {ROL_SUPERADMIN, ROL_ADMIN, ROL_SUPERVISOR}   # pueden registrar marcajes

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")
oauth2_scheme_opcional = OAuth2PasswordBearer(tokenUrl="/login", auto_error=False)

DEVICE_TOKEN_HEADER = "X-Device-Token"


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def rol_de(usuario: models.UsuarioAdmin) -> str:
    return (usuario.rol or "").strip().lower()


def authenticate_user(db: Session, email: str, password: str):
    user = db.query(models.UsuarioAdmin).filter(models.UsuarioAdmin.email == email).first()
    if not user or not verify_password(password, user.hash_password):
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
    if user is None:
        raise credentials_exception
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
            raise HTTPException(status_code=500, detail="Usuario sin empresa asignada")
        return ActorMarcaje(empresa_id=usuario.empresa_id, usuario=usuario)

    if x_device_token:
        dispositivo = (
            db.query(models.Dispositivo)
            .filter(models.Dispositivo.token_hash == hash_token_dispositivo(x_device_token))
            .first()
        )
        if not dispositivo:
            raise HTTPException(status_code=401, detail="Token de dispositivo inválido")
        if not dispositivo.activo:
            raise HTTPException(status_code=403, detail="Dispositivo desactivado")
        return ActorMarcaje(empresa_id=dispositivo.empresa_id, dispositivo=dispositivo)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Se requiere sesión de usuario o el header {DEVICE_TOKEN_HEADER}",
        headers={"WWW-Authenticate": "Bearer"},
    )
