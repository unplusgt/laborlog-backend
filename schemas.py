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
    foto_registro_path: Optional[str] = None
    activo: Optional[bool] = True

class TrabajadorCreate(TrabajadorBase):
    pass

class Trabajador(TrabajadorBase):
    id: uuid.UUID
    empresa_id: Optional[uuid.UUID] = None

    model_config = ConfigDict(from_attributes=True)

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

# UsuarioAdmin (nunca se expone hash_password)
class UsuarioAdminBase(BaseModel):
    nombre: str
    email: str
    rol: Optional[str] = None

class UsuarioAdminCreate(UsuarioAdminBase):
    password: str = Field(min_length=8)

class UsuarioAdminUpdate(BaseModel):
    nombre: Optional[str] = None
    email: Optional[str] = None
    rol: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=8)

class UsuarioAdmin(UsuarioAdminBase):
    id: uuid.UUID
    empresa_id: Optional[uuid.UUID] = None

    model_config = ConfigDict(from_attributes=True)

# Marcaje
class MarcajeCreate(BaseModel):
    trabajador_id: uuid.UUID
    obra_id: int
    tipo: str
    timestamp_local: datetime
    # Obligatorio cuando registra un usuario; con token de dispositivo se usa ese dispositivo
    origen: Optional[uuid.UUID] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    foto_path: Optional[str] = None
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

    model_config = ConfigDict(from_attributes=True)

# Alias para compatibilidad
Marcaje = MarcajeResponse


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
