from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Integer, LargeBinary, String, TIMESTAMP, UniqueConstraint,
)
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
    # Código corto que se escribe al iniciar sesión (p. ej. "constructora-abc")
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
    # Modo kiosco: obra donde está instalado el teléfono (solo puede marcar en esa obra)
    obra_id = Column(Integer, ForeignKey("obras.id"), nullable=True)

class Trabajador(Base):
    __tablename__ = "trabajadores"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    nombre = Column(String, nullable=False)
    dni = Column(String)
    telefono = Column(String)
    codigo_empresa = Column(String)
    # Obra a la que está asignado (define qué supervisor lo ve y en qué kiosco aparece)
    obra_id = Column(Integer, ForeignKey("obras.id"), nullable=True)
    fecha_registro = Column(TIMESTAMP)
    # Huella facial normalizada (float32 little-endian) del modelo indicado en embedding_modelo
    embedding = Column(LargeBinary)
    embedding_modelo = Column(String)
    foto_registro_path = Column(String)
    rostro_registrado_en = Column(TIMESTAMP)
    # Cuándo autorizó el trabajador el uso de su rostro (requisito para registrarlo)
    consentimiento_biometrico_en = Column(TIMESTAMP)
    activo = Column(Boolean, default=True)

    @property
    def rostro_registrado(self) -> bool:
        return self.embedding is not None

class UsuarioAdmin(Base):
    """Usuario que inicia sesión: plataforma (dueño de LaborLog, sin empresa),
    superadmin, jefe o supervisor (de una empresa)."""
    __tablename__ = "usuarios_admin"
    __table_args__ = (
        UniqueConstraint("empresa_id", "usuario", name="uq_usuarios_empresa_usuario"),
        UniqueConstraint("empresa_id", "email", name="uq_usuarios_empresa_email"),
    )
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id = Column(UUID(as_uuid=True), ForeignKey("empresas.id"), index=True)
    nombre = Column(String, nullable=False)
    # Nombre de usuario (único dentro de la empresa); se puede entrar con él o con el email
    usuario = Column(String, nullable=False)
    email = Column(String, nullable=True)
    hash_password = Column(String, nullable=False)
    rol = Column(String)
    activo = Column(Boolean, nullable=False, default=True)


class SupervisorObra(Base):
    """Obras asignadas a un supervisor (solo ve esas obras y sus trabajadores)."""
    __tablename__ = "supervisor_obras"
    usuario_id = Column(UUID(as_uuid=True), ForeignKey("usuarios_admin.id", ondelete="CASCADE"), primary_key=True)
    obra_id = Column(Integer, ForeignKey("obras.id", ondelete="CASCADE"), primary_key=True)

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
    # Precisión del GPS reportada por el teléfono (metros)
    precision_metros = Column(Float)
    # True si se capturó sin conexión y llegó después por POST /marcajes/lote
    capturado_offline = Column(Boolean, nullable=False, default=False)
    # rostro (reconocido por la cámara) | manual (eligió su nombre / lo marcó un supervisor)
    metodo = Column(String)
    # Similitud facial calculada en el teléfono y la recalculada por el servidor
    similitud = Column(Float)
    similitud_servidor = Column(Float)
    # Prueba de vida superada en el teléfono (p. ej. "parpadeo", "giro_izquierda")
    prueba_vida = Column(String)
