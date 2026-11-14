from sqlalchemy import Column, String, Integer, Boolean, TIMESTAMP, ForeignKey, LargeBinary, Float
from sqlalchemy.dialects.postgresql import UUID
import uuid
from .database import Base

# Cualquier cambio aquí necesita una migración nueva:
#   alembic revision -m "descripcion"   (y escribir upgrade/downgrade)

ZONA_HORARIA_DEFAULT = "America/Guatemala"


class Empresa(Base):
    __tablename__ = "empresas"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    activa = Column(Boolean, default=True)
    hora_extra_desde = Column(Integer, default=8)
    sobretiempo_factor = Column(Float, default=1.5)
    domingo_factor = Column(Float, default=2.0)
    festivo_factor = Column(Float, default=2.0)
    # Zona horaria IANA en la que se interpretan las horas locales de los marcajes
    zona_horaria = Column(String, nullable=False, default=ZONA_HORARIA_DEFAULT)
    created_at = Column(TIMESTAMP)

class Obra(Base):
    __tablename__ = "obras"
    id = Column(Integer, primary_key=True, index=True)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    nombre = Column(String, nullable=False)
    lat = Column(String)
    lng = Column(String)
    radio_metros = Column(Integer)

class Dispositivo(Base):
    __tablename__ = "dispositivos"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    nombre = Column(String)
    token_fcm = Column(String)
    last_sync = Column(TIMESTAMP)
    licencia = Column(String)
    # SHA-256 del token del dispositivo (el token en claro solo se muestra al crearlo/rotarlo)
    token_hash = Column(String, unique=True, index=True)
    activo = Column(Boolean, nullable=False, default=True)

class Trabajador(Base):
    __tablename__ = "trabajadores"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    nombre = Column(String, nullable=False)
    dni = Column(String)
    telefono = Column(String)
    codigo_empresa = Column(String)
    fecha_registro = Column(TIMESTAMP)
    embedding = Column(LargeBinary)
    foto_registro_path = Column(String)
    activo = Column(Boolean, default=True)

class UsuarioAdmin(Base):
    __tablename__ = "usuarios_admin"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    nombre = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    hash_password = Column(String, nullable=False)
    rol = Column(String)

class Marcaje(Base):
    __tablename__ = "marcajes"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    trabajador_id = Column(UUID(as_uuid=True), ForeignKey("trabajadores.id"))
    obra_id = Column(Integer, ForeignKey("obras.id"))
    tipo = Column(String)
    timestamp_server = Column(TIMESTAMP)
    timestamp_local = Column(TIMESTAMP, nullable=False)
    origen = Column(UUID(as_uuid=True), ForeignKey("dispositivos.id"))
    lat = Column(String)
    lng = Column(String)
    foto_path = Column(String)
    estado_sync = Column(String)
    razon_conflicto = Column(String)
    # Usuario (supervisor/admin) que registró el marcaje; NULL si vino de un dispositivo
    registrado_por = Column(UUID(as_uuid=True), ForeignKey("usuarios_admin.id"))
