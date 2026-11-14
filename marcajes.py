"""Reglas de negocio para registrar marcajes.

Todas las validaciones anti-fraude viven aquí, en el servidor. La app puede validar
lo mismo para dar mejor experiencia, pero el servidor es la fuente de verdad.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import ActorMarcaje

TIPOS_VALIDOS = ("entrada", "salida")

# Un turno (incluido nocturno) dura como máximo esto; pasado este tiempo una ENTRADA
# sin SALIDA se considera olvidada y se permite una nueva ENTRADA.
VENTANA_TURNO = timedelta(hours=20)

# Tolerancias de reloj del dispositivo respecto al servidor
TOLERANCIA_ADELANTO = timedelta(minutes=5)   # más adelantado que esto: se rechaza
TOLERANCIA_ATRASO = timedelta(minutes=15)    # más atrasado que esto: se acepta pero se marca conflicto

ESTADO_OK = "synced"
ESTADO_CONFLICTO = "conflicto"


def distancia_metros(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia entre dos coordenadas (fórmula de Haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _coordenada(valor, nombre: str, limite: float) -> float:
    try:
        numero = float(str(valor).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{nombre} inválida")
    if math.isnan(numero) or abs(numero) > limite:
        raise HTTPException(status_code=422, detail=f"{nombre} fuera de rango")
    return numero


def _zona_horaria(empresa: models.Empresa | None) -> ZoneInfo:
    nombre = (empresa.zona_horaria if empresa else None) or models.ZONA_HORARIA_DEFAULT
    try:
        return ZoneInfo(nombre)
    except ZoneInfoNotFoundError:
        return ZoneInfo(models.ZONA_HORARIA_DEFAULT)


def _validar_ubicacion(obra: models.Obra, payload: schemas.MarcajeCreate) -> None:
    if not obra.lat or not obra.lng or not str(obra.lat).strip() or not str(obra.lng).strip():
        raise HTTPException(
            status_code=422,
            detail=f'La obra "{obra.nombre}" no tiene ubicación GPS configurada',
        )
    if not obra.radio_metros or obra.radio_metros <= 0:
        raise HTTPException(
            status_code=422,
            detail=f'La obra "{obra.nombre}" no tiene radio permitido configurado',
        )
    if payload.lat is None or payload.lng is None:
        raise HTTPException(status_code=422, detail="El marcaje requiere ubicación GPS")

    obra_lat = _coordenada(obra.lat, "Latitud de la obra", 90)
    obra_lng = _coordenada(obra.lng, "Longitud de la obra", 180)
    lat = _coordenada(payload.lat, "Latitud", 90)
    lng = _coordenada(payload.lng, "Longitud", 180)

    distancia = distancia_metros(lat, lng, obra_lat, obra_lng)
    if distancia > obra.radio_metros:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Fuera del área de la obra: está a {distancia:.0f} m "
                f"(máximo permitido {obra.radio_metros} m)"
            ),
        )


def _hora_local(payload_ts: datetime, tz: ZoneInfo) -> datetime:
    """Normaliza la hora del dispositivo a hora local de la empresa, sin tzinfo."""
    if payload_ts.tzinfo is not None:
        return payload_ts.astimezone(tz).replace(tzinfo=None)
    return payload_ts


def registrar_marcaje(
    db: Session,
    payload: schemas.MarcajeCreate,
    actor: ActorMarcaje,
    ahora_utc: datetime | None = None,
) -> models.Marcaje:
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    empresa_id = actor.empresa_id

    tipo = (payload.tipo or "").strip().lower()
    if tipo not in TIPOS_VALIDOS:
        raise HTTPException(status_code=422, detail="tipo debe ser 'entrada' o 'salida'")

    # --- Pertenencia a la empresa -------------------------------------------
    trabajador = (
        db.query(models.Trabajador)
        .filter(models.Trabajador.id == payload.trabajador_id)
        .filter(models.Trabajador.empresa_id == empresa_id)
        .first()
    )
    if not trabajador:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    if trabajador.activo is False:
        raise HTTPException(status_code=409, detail="El trabajador está inactivo")

    obra = (
        db.query(models.Obra)
        .filter(models.Obra.id == payload.obra_id)
        .filter(models.Obra.empresa_id == empresa_id)
        .first()
    )
    if not obra:
        raise HTTPException(status_code=404, detail="Obra no encontrada")

    if actor.dispositivo is not None:
        dispositivo = actor.dispositivo
    else:
        if payload.origen is None:
            raise HTTPException(status_code=422, detail="Falta el dispositivo de origen")
        dispositivo = (
            db.query(models.Dispositivo)
            .filter(models.Dispositivo.id == payload.origen)
            .filter(models.Dispositivo.empresa_id == empresa_id)
            .first()
        )
        if not dispositivo:
            raise HTTPException(status_code=404, detail="Dispositivo no encontrado")
        if dispositivo.activo is False:
            raise HTTPException(status_code=403, detail="Dispositivo desactivado")

    # --- Ubicación (geocerca) ------------------------------------------------
    _validar_ubicacion(obra, payload)

    # --- Hora -----------------------------------------------------------------
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    tz = _zona_horaria(empresa)
    ts_local = _hora_local(payload.timestamp_local, tz)
    ahora_local = ahora_utc.astimezone(tz).replace(tzinfo=None)
    desfase = ts_local - ahora_local

    estado = ESTADO_OK
    razon = None
    if desfase > TOLERANCIA_ADELANTO:
        raise HTTPException(
            status_code=422,
            detail=(
                f"La hora del dispositivo está adelantada {int(desfase.total_seconds() // 60)} min. "
                "Corrige la fecha/hora del teléfono."
            ),
        )
    if desfase < -TOLERANCIA_ATRASO:
        estado = ESTADO_CONFLICTO
        razon = (
            f"Hora del dispositivo {int(-desfase.total_seconds() // 60)} min atrasada "
            "respecto al servidor"
        )

    # --- Secuencia entrada/salida (soporta turnos nocturnos) -----------------
    ultimo = (
        db.query(models.Marcaje)
        .filter(models.Marcaje.trabajador_id == trabajador.id)
        .filter(models.Marcaje.timestamp_local <= ts_local)
        .filter(models.Marcaje.timestamp_local >= ts_local - VENTANA_TURNO)
        .order_by(models.Marcaje.timestamp_local.desc())
        .first()
    )
    ultimo_tipo = (ultimo.tipo or "").lower() if ultimo else None
    esperado = "salida" if ultimo_tipo == "entrada" else "entrada"
    if tipo != esperado:
        if esperado == "salida":
            detalle = (
                f"{trabajador.nombre} ya tiene una ENTRADA a las "
                f"{ultimo.timestamp_local:%H:%M}; debe marcar SALIDA"
            )
        else:
            detalle = f"{trabajador.nombre} no tiene una ENTRADA abierta; debe marcar ENTRADA"
        raise HTTPException(status_code=409, detail=detalle)

    marcaje = models.Marcaje(
        id=uuid.uuid4(),
        empresa_id=empresa_id,
        trabajador_id=trabajador.id,
        obra_id=obra.id,
        tipo=tipo,
        timestamp_server=ahora_utc.replace(tzinfo=None),  # UTC
        timestamp_local=ts_local,
        origen=dispositivo.id,
        lat=str(payload.lat).strip(),
        lng=str(payload.lng).strip(),
        foto_path=payload.foto_path,
        estado_sync=estado,
        razon_conflicto=razon,
        registrado_por=actor.usuario.id if actor.usuario else None,
    )
    db.add(marcaje)
    if actor.dispositivo is not None:
        dispositivo.last_sync = ahora_utc.replace(tzinfo=None)
    db.commit()
    db.refresh(marcaje)
    return marcaje
