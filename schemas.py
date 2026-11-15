from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
import uuid
from datetime import datetime


class EmpresaBase(BaseModel):
    nombre: str
    slug: str
    activa: bool = True
    hora_extra_desde: int = 8
    sobretiempo_factor: float = 1.5
    domingo_factor: float = 2.0
    festivo_factor: float = 2.0
    zona_horaria: str = "America/Guatemala"


class Empresa(EmpresaBase):
    id: uuid.UUID

    model_config = ConfigDict(from_attributes=True)


class EmpresaConfigUpdate(BaseModel):
    hora_extra_desde: Optional[int] = None
    sobretiempo_factor: Optional[float] = None
    domingo_factor: Optional[float] = None
    festivo_factor: Optional[float] = None
    zona_horaria: Optional[str] = None

# Trabajador
class TrabajadorBase(BaseModel):
    nombre: str
    dni: Optional[str] = None
    telefono: Optional[str] = None
    codigo_empresa: Optional[str] = None
    activo: Optional[bool] = True
    # Obra asignada (la ve el supervisor de esa obra y aparece en su kiosco)
    obra_id: Optional[int] = None

class TrabajadorCreate(TrabajadorBase):
    pass

class Trabajador(TrabajadorBase):
    id: uuid.UUID
    empresa_id: Optional[uuid.UUID] = None
    # La huella facial nunca se expone aquí; solo si está registrada
    rostro_registrado: bool = False
    rostro_registrado_en: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class RostroRegistro(BaseModel):
    """Registro del rostro de un trabajador, capturado con el teléfono."""
    # 128 float32 little-endian en base64 (promedio de varias capturas)
    huella_b64: str = Field(max_length=1000)
    # Cara alineada en JPEG (base64), queda como foto de referencia
    foto_jpeg_b64: str = Field(max_length=220_000)
    # El trabajador autorizó el uso de su rostro para control de asistencia
    consentimiento: bool
    modelo: str

# Obra
class ObraBase(BaseModel):
    nombre: str
    lat: Optional[str] = None
    lng: Optional[str] = None
    radio_metros: Optional[int] = None

class ObraCreate(ObraBase):
    pass

class Obra(ObraBase):
    id: int
    empresa_id: Optional[uuid.UUID] = None

    model_config = ConfigDict(from_attributes=True)

# Dispositivo
class DispositivoBase(BaseModel):
    nombre: Optional[str] = None
    token_fcm: Optional[str] = None
    licencia: Optional[str] = None
    # Modo kiosco: obra donde está instalado el teléfono
    obra_id: Optional[int] = None

class DispositivoCreate(DispositivoBase):
    pass

class DispositivoUpdate(DispositivoBase):
    activo: Optional[bool] = None

class Dispositivo(DispositivoBase):
    id: uuid.UUID
    empresa_id: Optional[uuid.UUID] = None
    last_sync: Optional[datetime] = None
    activo: bool = True

    model_config = ConfigDict(from_attributes=True)

class DispositivoConToken(Dispositivo):
    """Respuesta al crear o rotar el token: es la ÚNICA vez que se ve el token en claro."""
    token: str

# Usuarios (nunca se expone hash_password)
PATRON_USUARIO = r"^[a-z0-9][a-z0-9._-]{2,39}$"
PATRON_CODIGO_EMPRESA = r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$"

class UsuarioAdminCreate(BaseModel):
    nombre: str = Field(min_length=1)
    # Para iniciar sesión: minúsculas, números, punto, guion o guion bajo
    usuario: str = Field(pattern=PATRON_USUARIO)
    email: Optional[str] = None
    rol: str
    password: str = Field(min_length=8)
    # Solo supervisores: obras que puede ver
    obras: list[int] = []

class UsuarioAdminUpdate(BaseModel):
    nombre: Optional[str] = None
    usuario: Optional[str] = Field(default=None, pattern=PATRON_USUARIO)
    email: Optional[str] = None
    rol: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=8)
    activo: Optional[bool] = None
    obras: Optional[list[int]] = None

class UsuarioAdmin(BaseModel):
    id: uuid.UUID
    empresa_id: Optional[uuid.UUID] = None
    nombre: str
    usuario: str
    email: Optional[str] = None
    rol: Optional[str] = None
    activo: bool = True
    obras: list[int] = []

    model_config = ConfigDict(from_attributes=True)


# Plataforma (dueño de LaborLog): alta y administración de empresas
class PrimerUsuario(BaseModel):
    nombre: str = Field(min_length=1)
    usuario: str = Field(pattern=PATRON_USUARIO)
    email: Optional[str] = None
    password: str = Field(min_length=8)

class EmpresaNueva(BaseModel):
    nombre: str = Field(min_length=1)
    codigo: str = Field(pattern=PATRON_CODIGO_EMPRESA)
    zona_horaria: str = "America/Guatemala"
    superadmin: PrimerUsuario

class EmpresaPlataformaUpdate(BaseModel):
    nombre: Optional[str] = None
    codigo: Optional[str] = Field(default=None, pattern=PATRON_CODIGO_EMPRESA)
    activa: Optional[bool] = None

class EmpresaPlataforma(BaseModel):
    id: uuid.UUID
    nombre: str
    codigo: str
    activa: bool
    zona_horaria: str
    created_at: Optional[datetime] = None
    usuarios: int = 0
    trabajadores: int = 0
    obras: int = 0
    dispositivos: int = 0

# Marcaje
class MarcajeCreate(BaseModel):
    # Lo genera el teléfono al capturar el marcaje; permite reenviar sin duplicar
    id: Optional[uuid.UUID] = None
    trabajador_id: uuid.UUID
    obra_id: int
    tipo: str
    timestamp_local: datetime
    # Obligatorio cuando registra un usuario; con token de dispositivo se usa ese dispositivo
    origen: Optional[uuid.UUID] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    precision_metros: Optional[float] = Field(default=None, ge=0)
    # El teléfono la reporta si detecta una app de GPS falso (Android: isMocked)
    ubicacion_simulada: bool = False
    # Reconocimiento facial (Fase 2)
    metodo: Optional[str] = None             # rostro | manual
    similitud: Optional[float] = Field(default=None, ge=-1, le=1)
    prueba_vida: Optional[str] = Field(default=None, max_length=40)
    foto_jpeg_b64: Optional[str] = Field(default=None, max_length=220_000)
    # estado_sync y razon_conflicto los decide el servidor; se ignoran si vienen del cliente

class MarcajeResponse(BaseModel):
    id: uuid.UUID
    empresa_id: Optional[uuid.UUID] = None
    trabajador_id: uuid.UUID
    obra_id: int
    tipo: str
    timestamp_server: Optional[datetime] = None
    timestamp_local: datetime
    origen: Optional[uuid.UUID] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    foto_path: Optional[str] = None
    estado_sync: Optional[str] = None
    razon_conflicto: Optional[str] = None
    registrado_por: Optional[uuid.UUID] = None
    precision_metros: Optional[float] = None
    capturado_offline: bool = False
    metodo: Optional[str] = None
    similitud: Optional[float] = None
    similitud_servidor: Optional[float] = None
    prueba_vida: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# Alias para compatibilidad
Marcaje = MarcajeResponse


# Envío por lotes desde el teléfono (cola offline)
MAX_MARCAJES_POR_LOTE = 500

class MarcajeLote(BaseModel):
    # Hora del teléfono al momento de enviar: sirve para corregir el desfase de su reloj
    enviado_en: datetime
    marcajes: list[MarcajeCreate] = Field(max_length=MAX_MARCAJES_POR_LOTE)

class ResultadoMarcaje(BaseModel):
    id: Optional[uuid.UUID] = None
    # creado | duplicado (ya se había recibido) | rechazado
    estado: str
    codigo: int
    error: Optional[str] = None
    marcaje: Optional[MarcajeResponse] = None

class ResultadoLote(BaseModel):
    hora_servidor: datetime
    resultados: list[ResultadoMarcaje]


# Configuración que descarga el teléfono en modo kiosco
class KioscoTrabajador(BaseModel):
    id: uuid.UUID
    nombre: str
    codigo_empresa: Optional[str] = None
    # Último marcaje dentro de la ventana de turno (para proponer ENTRADA o SALIDA sin conexión)
    ultimo_tipo: Optional[str] = None
    ultimo_marcaje: Optional[datetime] = None  # UTC
    # Huella facial (base64) para reconocer sin conexión; None si no tiene rostro registrado
    huella_b64: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class KioscoObra(BaseModel):
    id: int
    nombre: str
    lat: Optional[str] = None
    lng: Optional[str] = None
    radio_metros: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

class KioscoConfig(BaseModel):
    dispositivo_id: uuid.UUID
    dispositivo_nombre: Optional[str] = None
    empresa_nombre: str
    # Para pedir usuario de supervisor en el kiosco sin escribir la empresa
    empresa_codigo: Optional[str] = None
    zona_horaria: str
    hora_servidor: datetime
    obra: Optional[KioscoObra] = None
    trabajadores: list[KioscoTrabajador]


class PlanillaResumenItem(BaseModel):
    trabajador_id: uuid.UUID
    trabajador_nombre: str
    horas_normales: float
    horas_extra: float
    pago_estimado: float


class PlanillaResumenResponse(BaseModel):
    fecha_inicio: datetime
    fecha_fin: datetime
    total_horas_normales: float
    total_horas_extra: float
    total_pago_estimado: float
    items: list[PlanillaResumenItem]
