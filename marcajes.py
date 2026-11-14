"""Reglas de negocio para registrar marcajes.

Todas las validaciones anti-fraude viven aquí, en el servidor. La app puede validar
lo mismo para dar mejor experiencia, pero el servidor es la fuente de verdad.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import fotos, models, rostros, schemas
from .auth import ActorMarcaje

TIPOS_VALIDOS = ("entrada", "salida")
METODOS_VALIDOS = ("rostro", "manual")

# Un turno (incluido nocturno) dura como máximo esto; pasado este tiempo una ENTRADA
# sin SALIDA se considera olvidada y se permite una nueva ENTRADA.
VENTANA_TURNO = timedelta(hours=20)

# Tolerancias de reloj del dispositivo respecto al servidor
TOLERANCIA_ADELANTO = timedelta(minutes=5)   # más adelantado que esto: se rechaza
TOLERANCIA_ATRASO = timedelta(minutes=15)    # más atrasado que esto: se acepta pero se marca conflicto

# Marcajes capturados sin conexión (POST /marcajes/lote)
TOLERANCIA_RELOJ_OFFLINE = timedelta(minutes=5)  # desfase del reloj que se corrige pero se marca conflicto
MAX_RETRASO_OFFLINE = timedelta(days=7)          # llegar más tarde que esto se marca conflicto

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


def zona_horaria_empresa(empresa: models.Empresa | None) -> ZoneInfo:
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


def _minutos(delta: timedelta) -> int:
    return int(abs(delta.total_seconds()) // 60)


def marcaje_existente(db: Session, marcaje_id, empresa_id) -> models.Marcaje | None:
    """Si el teléfono reenvía un marcaje que ya llegó (mismo id), se devuelve el existente."""
    if marcaje_id is None:
        return None
    existente = db.query(models.Marcaje).filter(models.Marcaje.id == marcaje_id).first()
    if existente is None:
        return None
    if existente.empresa_id != empresa_id:
        raise HTTPException(status_code=409, detail="El id del marcaje ya está en uso")
    return existente


def _validar_hora_online(ts_local: datetime, ahora_local: datetime):
    """Marcaje enviado en el momento: el reloj del teléfono debe coincidir con el servidor."""
    desfase = ts_local - ahora_local
    if desfase > TOLERANCIA_ADELANTO:
        raise HTTPException(
            status_code=422,
            detail=(
                f"La hora del dispositivo está adelantada {_minutos(desfase)} min. "
                "Corrige la fecha/hora del teléfono."
            ),
        )
    if desfase < -TOLERANCIA_ATRASO:
        return f"Hora del dispositivo {_minutos(desfase)} min atrasada respecto al servidor"
    return None


def _corregir_hora_offline(ts_dispositivo: datetime, enviado_local: datetime, ahora_local: datetime):
    """Marcaje capturado sin conexión y enviado después.

    El teléfono manda la hora de captura y la hora de envío, ambas según SU reloj. La
    diferencia entre su hora de envío y la del servidor es el desfase de su reloj; se
    aplica a la hora de captura. Así un teléfono con la hora mal puesta no altera las
    horas trabajadas (salvo que cambien el reloj entre la captura y el envío).
    """
    desfase_reloj = ahora_local - enviado_local
    ts_local = ts_dispositivo + desfase_reloj
    if ts_local - ahora_local > TOLERANCIA_ADELANTO:
        raise HTTPException(status_code=422, detail="El marcaje tiene una hora futura")
    razones = []
    if abs(desfase_reloj) > TOLERANCIA_RELOJ_OFFLINE:
        sentido = "atrasado" if desfase_reloj > timedelta(0) else "adelantado"
        razones.append(
            f"Reloj del dispositivo {sentido} {_minutos(desfase_reloj)} min; hora corregida por el servidor"
        )
    retraso = ahora_local - ts_local
    if retraso > MAX_RETRASO_OFFLINE:
        razones.append(f"Sincronizado {retraso.days} días después de capturado")
    return ts_local, ("; ".join(razones) or None)


def registrar_marcaje(
    db: Session,
    payload: schemas.MarcajeCreate,
    actor: ActorMarcaje,
    ahora_utc: datetime | None = None,
    enviado_en: datetime | None = None,
) -> models.Marcaje:
    """Valida y guarda un marcaje.

    enviado_en: solo para marcajes capturados sin conexión (POST /marcajes/lote); es la
    hora del teléfono al enviar el lote y permite corregir el desfase de su reloj.
    """
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    empresa_id = actor.empresa_id

    existente = marcaje_existente(db, payload.id, empresa_id)
    if existente is not None:
        return existente

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

    if actor.dispositivo is not None and actor.dispositivo.obra_id is not None:
        if obra.id != actor.dispositivo.obra_id:
            raise HTTPException(status_code=403, detail="Este dispositivo está asignado a otra obra")

    # --- Ubicación (geocerca) ------------------------------------------------
    if payload.ubicacion_simulada:
        raise HTTPException(
            status_code=422,
            detail="Se detectó una ubicación simulada (GPS falso). Desactiva la app de ubicación falsa.",
        )
    _validar_ubicacion(obra, payload)

    # --- Hora -----------------------------------------------------------------
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    tz = zona_horaria_empresa(empresa)
    ts_dispositivo = _hora_local(payload.timestamp_local, tz)
    ahora_local = ahora_utc.astimezone(tz).replace(tzinfo=None)
    if enviado_en is None:
        ts_local = ts_dispositivo
        razon = _validar_hora_online(ts_local, ahora_local)
    else:
        ts_local, razon = _corregir_hora_offline(
            ts_dispositivo, _hora_local(enviado_en, tz), ahora_local
        )
    razones = [razon] if razon else []

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

    # Un marcaje offline puede llegar después de otros más recientes: no puede dejar
    # dos entradas (o dos salidas) seguidas.
    siguiente = (
        db.query(models.Marcaje)
        .filter(models.Marcaje.trabajador_id == trabajador.id)
        .filter(models.Marcaje.timestamp_local > ts_local)
        .filter(models.Marcaje.timestamp_local <= ts_local + VENTANA_TURNO)
        .order_by(models.Marcaje.timestamp_local.asc())
        .first()
    )
    if siguiente is not None and (siguiente.tipo or "").lower() == tipo:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{trabajador.nombre} ya tiene una {tipo.upper()} posterior "
                f"({siguiente.timestamp_local:%d/%m %H:%M})"
            ),
        )

    # --- Reconocimiento facial ------------------------------------------------
    metodo = (payload.metodo or "manual").strip().lower()
    if metodo not in METODOS_VALIDOS:
        raise HTTPException(status_code=422, detail="metodo debe ser 'rostro' o 'manual'")
    foto = None
    if payload.foto_jpeg_b64:
        try:
            foto = rostros.foto_de_base64(payload.foto_jpeg_b64)
        except rostros.RostroInvalido as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    similitud_servidor, razon_rostro = _verificar_rostro(metodo, foto, trabajador, actor)
    if razon_rostro:
        razones.append(razon_rostro)
    estado = ESTADO_CONFLICTO if razones else ESTADO_OK
    razon = "; ".join(razones) or None

    marcaje_id = payload.id or uuid.uuid4()
    foto_path = fotos.guardar(empresa_id, "marcajes", marcaje_id, foto, ts_local) if foto else None

    marcaje = models.Marcaje(
        id=marcaje_id,
        empresa_id=empresa_id,
        trabajador_id=trabajador.id,
        obra_id=obra.id,
        tipo=tipo,
        timestamp_server=ahora_utc.replace(tzinfo=None),  # UTC
        timestamp_local=ts_local,
        origen=dispositivo.id,
        lat=str(payload.lat).strip(),
        lng=str(payload.lng).strip(),
        foto_path=foto_path,
        estado_sync=estado,
        razon_conflicto=razon,
        registrado_por=actor.usuario.id if actor.usuario else None,
        precision_metros=payload.precision_metros,
        capturado_offline=enviado_en is not None,
        metodo=metodo,
        similitud=payload.similitud if metodo == "rostro" else None,
        similitud_servidor=similitud_servidor,
        prueba_vida=payload.prueba_vida if metodo == "rostro" else None,
    )
    db.add(marcaje)
    if actor.dispositivo is not None:
        dispositivo.last_sync = ahora_utc.replace(tzinfo=None)
    try:
        db.commit()
    except IntegrityError:
        # Otro envío con el mismo id llegó al mismo tiempo
        db.rollback()
        fotos.borrar(foto_path)
        existente = marcaje_existente(db, payload.id, empresa_id)
        if existente is None:
            raise
        return existente
    db.refresh(marcaje)
    return marcaje


def _verificar_rostro(metodo, foto, trabajador, actor):
    """Devuelve (similitud calculada por el servidor, motivo de conflicto o None).

    Nunca rechaza el marcaje: si algo no cuadra queda como conflicto para que el
    supervisor lo revise con la foto."""
    if metodo == "manual":
        if actor.dispositivo is not None and trabajador.embedding is not None:
            return None, "Marcado sin reconocimiento facial (el trabajador tiene rostro registrado)"
        return None, None
    # metodo == "rostro"
    if foto is None:
        return None, "Marcaje facial sin foto"
    if trabajador.embedding is None:
        return None, "El trabajador no tiene rostro registrado"
    motor = rostros.embebedor()
    if motor is None:
        return None, None  # servidor sin modelo: se confía en el teléfono (queda en el log)
    try:
        sim = rostros.similitud(motor.huella(foto), rostros.de_bytes(trabajador.embedding))
    except rostros.RostroInvalido as exc:
        return None, f"Foto del marcaje ilegible: {exc}"
    if sim < rostros.UMBRAL_SIMILITUD:
        return sim, f"El rostro no coincide con {trabajador.nombre} (similitud {sim:.2f})"
    return sim, None


def registrar_lote(
    db: Session,
    lote: schemas.MarcajeLote,
    actor: ActorMarcaje,
    ahora_utc: datetime | None = None,
) -> list[schemas.ResultadoMarcaje]:
    """Registra la cola offline de un teléfono. Cada marcaje se valida por separado y
    el resultado indica si se creó, si ya existía (reenvío) o por qué se rechazó.
    Se procesan en orden cronológico para que la secuencia entrada/salida cuadre."""
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    resultados: list[schemas.ResultadoMarcaje] = []
    for payload in sorted(lote.marcajes, key=lambda m: _orden(m.timestamp_local)):
        try:
            ya_existia = marcaje_existente(db, payload.id, actor.empresa_id) is not None
            marcaje = registrar_marcaje(db, payload, actor, ahora_utc, enviado_en=lote.enviado_en)
            resultados.append(
                schemas.ResultadoMarcaje(
                    id=marcaje.id,
                    estado="duplicado" if ya_existia else "creado",
                    codigo=200,
                    marcaje=schemas.MarcajeResponse.model_validate(marcaje),
                )
            )
        except HTTPException as exc:
            db.rollback()
            resultados.append(
                schemas.ResultadoMarcaje(
                    id=payload.id, estado="rechazado", codigo=exc.status_code, error=str(exc.detail)
                )
            )
    return resultados


def _orden(ts: datetime) -> datetime:
    # Mezcla de horas con y sin zona horaria: se comparan como UTC / ingenuas
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts
