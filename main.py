import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import alcance, auth, fotos, models, rostros, schemas
from .auth import (
    CODIGO_PLATAFORMA, ROLES_EMPRESA, ROLES_GESTION, ROLES_MARCAJE, ROLES_PLANILLA,
    ROL_JEFE, ROL_PLATAFORMA, ROL_SUPERADMIN, ROL_SUPERVISOR,
)
from .database import get_db
from .marcajes import VENTANA_TURNO, zona_horaria_empresa, registrar_lote, registrar_marcaje

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("laborlog_backend")

app = FastAPI(title="LaborLog API")

# --- Frontend Flutter Web compilado (opcional) -------------------------------
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_docker_path = "/app/laborlog_app/build/web"
_local_path = os.path.join(_parent_dir, "laborlog_app", "build", "web")
static_path = _docker_path if os.path.exists(_docker_path) else _local_path
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")
    logger.info("Frontend estático montado desde %s", static_path)

# --- CORS ---------------------------------------------------------------------
_cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# El esquema de BD lo gestiona Alembic (`alembic upgrade head`), no el arranque de la API.


# --- Manejo global de errores -------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.warning("HTTPException %s %s | %s", exc.status_code, request.url.path, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "type": "HTTPException", "status_code": exc.status_code},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("Validation error %s | %s", request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation error",
            "details": jsonable_errors(exc.errors()),
            "type": "RequestValidationError",
            "status_code": 422,
        },
    )


def jsonable_errors(errors):
    # Pydantic puede incluir objetos no serializables en 'ctx'
    return [{k: (str(v) if k == "ctx" else v) for k, v in e.items() if k != "input"} for e in errors]


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.warning("HTTP %s %s | %s", exc.status_code, request.url.path, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "type": "StarletteHTTPException", "status_code": exc.status_code},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled Exception %s | %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "type": "Exception", "status_code": 500},
    )


# --- Helpers ------------------------------------------------------------------
# superadmin y jefe: catálogos, trabajadores, dispositivos, supervisores
gestion = auth.require_role(ROLES_GESTION)
# solo superadmin: planilla y configuración de pago
solo_superadmin = auth.require_role(ROLES_PLANILLA)
# cualquier usuario de una empresa (no el usuario de plataforma)
usuario_empresa = auth.require_role(ROLES_EMPRESA)
solo_plataforma = auth.require_role({ROL_PLATAFORMA})


def _empresa_id_or_500(current_user: models.UsuarioAdmin):
    if not current_user.empresa_id:
        raise HTTPException(status_code=403, detail="Usuario sin empresa asignada")
    return current_user.empresa_id


def _validar_obra_empresa(db: Session, obra_id, empresa_id):
    if obra_id is not None:
        _get_o_404(db, models.Obra, obra_id, empresa_id, "Obra")


def _get_o_404(db: Session, modelo, entity_id, empresa_id, nombre: str):
    obj = (
        db.query(modelo)
        .filter(modelo.id == entity_id)
        .filter(modelo.empresa_id == empresa_id)
        .first()
    )
    if not obj:
        raise HTTPException(status_code=404, detail=f"{nombre} not found")
    return obj


def _build_planilla_resumen(db: Session, empresa_id, fecha_inicio: datetime, fecha_fin: datetime):
    marcajes = (
        db.query(models.Marcaje)
        .join(models.Trabajador, models.Trabajador.id == models.Marcaje.trabajador_id)
        .filter(models.Marcaje.empresa_id == empresa_id)
        .filter(models.Marcaje.timestamp_local >= fecha_inicio)
        .filter(models.Marcaje.timestamp_local <= fecha_fin)
        .order_by(models.Marcaje.trabajador_id, models.Marcaje.timestamp_local)
        .all()
    )

    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    hora_extra_desde = empresa.hora_extra_desde if empresa else 8

    trabajadores = {
        row.id: row.nombre
        for row in db.query(models.Trabajador).filter(models.Trabajador.empresa_id == empresa_id).all()
    }

    marcajes_por_trabajador_dia = defaultdict(list)
    for m in marcajes:
        marcajes_por_trabajador_dia[(m.trabajador_id, m.timestamp_local.date())].append(m)

    resumen = defaultdict(lambda: {"normales": 0.0, "extra": 0.0})
    for (trabajador_id, _dia), items in marcajes_por_trabajador_dia.items():
        entradas = [x for x in items if (x.tipo or "").lower() == "entrada"]
        salidas = [x for x in items if (x.tipo or "").lower() == "salida"]
        if not entradas or not salidas:
            continue

        inicio = min(x.timestamp_local for x in entradas)
        fin = max(x.timestamp_local for x in salidas)
        horas = max(0.0, (fin - inicio).total_seconds() / 3600)
        normales = min(horas, float(hora_extra_desde))
        extra = max(0.0, horas - float(hora_extra_desde))

        resumen[trabajador_id]["normales"] += normales
        resumen[trabajador_id]["extra"] += extra

    items = []
    total_normales = 0.0
    total_extra = 0.0
    total_pago = 0.0

    for trabajador_id, value in resumen.items():
        horas_normales = round(value["normales"], 2)
        horas_extra = round(value["extra"], 2)
        pago_estimado = round((horas_normales * 1.0) + (horas_extra * 1.5), 2)
        total_normales += horas_normales
        total_extra += horas_extra
        total_pago += pago_estimado
        items.append(
            schemas.PlanillaResumenItem(
                trabajador_id=trabajador_id,
                trabajador_nombre=trabajadores.get(trabajador_id, "Trabajador"),
                horas_normales=horas_normales,
                horas_extra=horas_extra,
                pago_estimado=pago_estimado,
            )
        )

    return schemas.PlanillaResumenResponse(
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        total_horas_normales=round(total_normales, 2),
        total_horas_extra=round(total_extra, 2),
        total_pago_estimado=round(total_pago, 2),
        items=items,
    )


# =============================================================================
# AUTH
# =============================================================================

@app.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    empresa: str = Form(""),
    db: Session = Depends(get_db),
):
    """Inicia sesión con código de empresa + usuario (o email) + contraseña.
    El dueño de la plataforma usa el código de empresa "plataforma".
    (En /docs, si no hay campo empresa, se acepta username "empresa/usuario".)"""
    if not empresa and "/" in username:
        empresa, username = username.split("/", 1)
    user = auth.authenticate_user(db, empresa, username, password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empresa, usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth.create_access_token(data={"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/me")
def read_users_me(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user),
):
    empresa = None
    if current_user.empresa_id:
        empresa = db.query(models.Empresa).filter(models.Empresa.id == current_user.empresa_id).first()
    return {
        "user": current_user.email or current_user.usuario,
        "id": str(current_user.id),
        "nombre": current_user.nombre,
        "usuario": current_user.usuario,
        "email": current_user.email,
        "rol": auth.rol_de(current_user),
        "empresa_id": str(current_user.empresa_id) if current_user.empresa_id else None,
        "empresa_codigo": empresa.slug if empresa else None,
        "empresa_nombre": empresa.nombre if empresa else None,
        # Supervisor: obras que puede ver (None = todas)
        "obras": alcance.obras_visibles(db, current_user),
    }


@app.get("/empresas/me", response_model=schemas.Empresa)
def get_empresa_actual(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa),
):
    empresa_id = _empresa_id_or_500(current_user)
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="Empresa not found")
    return empresa


@app.put("/empresas/me/config", response_model=schemas.Empresa)
def update_empresa_config(
    payload: schemas.EmpresaConfigUpdate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_superadmin),
):
    empresa_id = _empresa_id_or_500(current_user)
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="Empresa not found")

    cambios = payload.model_dump(exclude_unset=True)
    if "zona_horaria" in cambios:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(cambios["zona_horaria"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=422, detail="Zona horaria inválida (usar formato IANA, ej. America/Guatemala)")

    for field, value in cambios.items():
        setattr(empresa, field, value)

    db.commit()
    db.refresh(empresa)
    return empresa

# =============================================================================
# TRABAJADORES (lectura: cualquier usuario; escritura: admin)
# =============================================================================

@app.get("/trabajadores", response_model=list[schemas.Trabajador])
@app.get("/trabajadores/", response_model=list[schemas.Trabajador], include_in_schema=False)
def get_trabajadores(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    q = db.query(models.Trabajador).filter(models.Trabajador.empresa_id == _empresa_id_or_500(current_user))
    return alcance.filtrar_trabajadores(q, db, current_user).order_by(models.Trabajador.nombre).all()

@app.get("/trabajadores/{trabajador_id}", response_model=schemas.Trabajador)
def get_trabajador(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    t = _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")
    alcance.exigir_trabajador(db, current_user, t)
    return t

@app.post("/trabajadores", response_model=schemas.Trabajador)
@app.post("/trabajadores/", response_model=schemas.Trabajador, include_in_schema=False)
def create_trabajador(
    trabajador: schemas.TrabajadorCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    empresa_id = _empresa_id_or_500(current_user)
    _validar_obra_empresa(db, trabajador.obra_id, empresa_id)
    db_trabajador = models.Trabajador(
        **trabajador.model_dump(),
        empresa_id=empresa_id,
        fecha_registro=datetime.now(),
    )
    db.add(db_trabajador)
    db.commit()
    db.refresh(db_trabajador)
    return db_trabajador

@app.put("/trabajadores/{trabajador_id}", response_model=schemas.Trabajador)
def update_trabajador(
    trabajador_id: uuid.UUID,
    trabajador: schemas.TrabajadorCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    empresa_id = _empresa_id_or_500(current_user)
    db_trabajador = _get_o_404(db, models.Trabajador, trabajador_id, empresa_id, "Trabajador")
    cambios = trabajador.model_dump(exclude_unset=True)
    if "obra_id" in cambios:
        _validar_obra_empresa(db, cambios["obra_id"], empresa_id)
    for field, value in cambios.items():
        setattr(db_trabajador, field, value)
    db.commit()
    db.refresh(db_trabajador)
    return db_trabajador

@app.delete("/trabajadores/{trabajador_id}")
def delete_trabajador(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    db_trabajador = _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")
    db.delete(db_trabajador)
    db.commit()
    return {"detail": "Trabajador deleted successfully"}

# =============================================================================
# ROSTROS (registro: supervisor o admin; borrar: admin)
# =============================================================================

supervisor_o_admin = auth.require_role(ROLES_MARCAJE)

# Dos trabajadores distintos no pueden tener caras tan parecidas (evita que alguien
# registre su rostro en la ficha de otro para marcar por él)
UMBRAL_DUPLICADO = 0.55

@app.put("/trabajadores/{trabajador_id}/rostro", response_model=schemas.Trabajador)
def registrar_rostro(
    trabajador_id: uuid.UUID,
    datos: schemas.RostroRegistro,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(supervisor_o_admin),
):
    """Registra (o reemplaza) el rostro del trabajador. Requiere su autorización."""
    empresa_id = _empresa_id_or_500(current_user)
    trabajador = _get_o_404(db, models.Trabajador, trabajador_id, empresa_id, "Trabajador")
    alcance.exigir_trabajador(db, current_user, trabajador)
    if not datos.consentimiento:
        raise HTTPException(
            status_code=422,
            detail="Se requiere la autorización del trabajador para registrar su rostro",
        )
    if datos.modelo != rostros.MODELO:
        raise HTTPException(status_code=422, detail=f"Modelo facial no soportado: {datos.modelo}")
    try:
        huella = rostros.de_base64(datos.huella_b64)
        foto = rostros.foto_de_base64(datos.foto_jpeg_b64)
        motor = rostros.embebedor()
        if motor is not None:
            sim = rostros.similitud(motor.huella(foto), huella)
            if sim < rostros.UMBRAL_REGISTRO:
                raise HTTPException(
                    status_code=422,
                    detail=f"La foto no corresponde a la huella enviada (similitud {sim:.2f}). Repite la captura.",
                )
    except rostros.RostroInvalido as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    otros = (
        db.query(models.Trabajador)
        .filter(models.Trabajador.empresa_id == empresa_id)
        .filter(models.Trabajador.id != trabajador.id)
        .filter(models.Trabajador.embedding.is_not(None))
        .all()
    )
    for otro in otros:
        if otro.embedding_modelo == rostros.MODELO:
            sim = rostros.similitud(huella, rostros.de_bytes(otro.embedding))
            if sim >= UMBRAL_DUPLICADO:
                raise HTTPException(
                    status_code=409,
                    detail=f"Este rostro se parece mucho al de {otro.nombre} (similitud {sim:.2f}). "
                    "Verifica que sea la persona correcta.",
                )

    ahora = datetime.now()
    anterior = trabajador.foto_registro_path
    trabajador.foto_registro_path = fotos.guardar(empresa_id, "rostros", trabajador.id, foto, ahora)
    if anterior and anterior != trabajador.foto_registro_path:
        fotos.borrar(anterior)
    trabajador.embedding = rostros.a_bytes(huella)
    trabajador.embedding_modelo = rostros.MODELO
    trabajador.rostro_registrado_en = ahora
    trabajador.consentimiento_biometrico_en = ahora
    db.commit()
    db.refresh(trabajador)
    logger.info("Rostro registrado trabajador=%s por=%s", trabajador.id, current_user.id)
    return trabajador

@app.delete("/trabajadores/{trabajador_id}/rostro", response_model=schemas.Trabajador)
def borrar_rostro(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion),
):
    """Elimina los datos biométricos del trabajador (huella y foto de referencia)."""
    trabajador = _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")
    fotos.borrar(trabajador.foto_registro_path)
    trabajador.embedding = None
    trabajador.embedding_modelo = None
    trabajador.foto_registro_path = None
    trabajador.rostro_registrado_en = None
    trabajador.consentimiento_biometrico_en = None
    db.commit()
    db.refresh(trabajador)
    return trabajador

def _respuesta_foto(ruta):
    datos = fotos.leer(ruta) if ruta else None
    if datos is None:
        raise HTTPException(status_code=404, detail="Sin foto")
    return Response(content=datos, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

@app.get("/trabajadores/{trabajador_id}/rostro/foto")
def foto_rostro(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa),
):
    trabajador = _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")
    alcance.exigir_trabajador(db, current_user, trabajador)
    return _respuesta_foto(trabajador.foto_registro_path)

@app.get("/marcajes/{marcaje_id}/foto")
def foto_marcaje(
    marcaje_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa),
):
    marcaje = _get_o_404(db, models.Marcaje, marcaje_id, _empresa_id_or_500(current_user), "Marcaje")
    alcance.exigir_obra(db, current_user, marcaje.obra_id)
    return _respuesta_foto(marcaje.foto_path)

# =============================================================================
# OBRAS (lectura: cualquier usuario; escritura: admin)
# =============================================================================

@app.get("/obras", response_model=list[schemas.Obra])
@app.get("/obras/", response_model=list[schemas.Obra], include_in_schema=False)
def get_obras(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    q = db.query(models.Obra).filter(models.Obra.empresa_id == _empresa_id_or_500(current_user))
    return alcance.filtrar_obras(q, db, current_user).order_by(models.Obra.nombre).all()

@app.get("/obras/{obra_id}", response_model=schemas.Obra)
def get_obra(
    obra_id: int,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    obra = _get_o_404(db, models.Obra, obra_id, _empresa_id_or_500(current_user), "Obra")
    alcance.exigir_obra(db, current_user, obra.id)
    return obra

@app.post("/obras", response_model=schemas.Obra)
@app.post("/obras/", response_model=schemas.Obra, include_in_schema=False)
def create_obra(
    obra: schemas.ObraCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    db_obra = models.Obra(**obra.model_dump(), empresa_id=_empresa_id_or_500(current_user))
    db.add(db_obra)
    db.commit()
    db.refresh(db_obra)
    return db_obra

@app.put("/obras/{obra_id}", response_model=schemas.Obra)
def update_obra(
    obra_id: int,
    obra: schemas.ObraCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    db_obra = _get_o_404(db, models.Obra, obra_id, _empresa_id_or_500(current_user), "Obra")
    for field, value in obra.model_dump(exclude_unset=True).items():
        setattr(db_obra, field, value)
    db.commit()
    db.refresh(db_obra)
    return db_obra

@app.delete("/obras/{obra_id}")
def delete_obra(
    obra_id: int,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    db_obra = _get_o_404(db, models.Obra, obra_id, _empresa_id_or_500(current_user), "Obra")
    db.delete(db_obra)
    db.commit()
    return {"detail": "Obra deleted successfully"}

# =============================================================================
# MARCAJES
# =============================================================================

@app.post("/marcajes", response_model=schemas.Marcaje)
@app.post("/marcajes/", response_model=schemas.Marcaje, include_in_schema=False)
def create_marcaje_endpoint(
    marcaje: schemas.MarcajeCreate,
    db: Session = Depends(get_db),
    actor: auth.ActorMarcaje = Depends(auth.get_actor_marcaje),
):
    """Registra un marcaje. Requiere sesión de supervisor/admin (Bearer) o
    el header X-Device-Token de un dispositivo registrado. Valida en el servidor
    la geocerca de la obra, la secuencia entrada/salida y la hora del dispositivo."""
    nuevo = registrar_marcaje(db, marcaje, actor)
    logger.info(
        "Marcaje %s %s trabajador=%s obra=%s estado=%s",
        nuevo.id, nuevo.tipo, nuevo.trabajador_id, nuevo.obra_id, nuevo.estado_sync,
    )
    return nuevo

@app.post("/marcajes/lote", response_model=schemas.ResultadoLote)
def create_marcajes_lote(
    lote: schemas.MarcajeLote,
    db: Session = Depends(get_db),
    actor: auth.ActorMarcaje = Depends(auth.get_actor_marcaje),
):
    """Recibe la cola de marcajes capturados sin conexión por un teléfono.

    - Cada marcaje lleva un `id` generado en el teléfono: reenviar el mismo lote no duplica.
    - `enviado_en` es la hora del teléfono al enviar; con ella el servidor corrige el
      desfase de su reloj en cada marcaje.
    - Siempre responde 200 con el resultado de cada marcaje (creado, duplicado o rechazado
      con el motivo). El teléfono debe quitar de su cola los creados y duplicados y
      mostrar los rechazados al supervisor.
    """
    ahora = datetime.now(timezone.utc)
    resultados = registrar_lote(db, lote, actor, ahora)
    if actor.dispositivo is not None:
        actor.dispositivo.last_sync = ahora.replace(tzinfo=None)
        db.commit()
    resumen = defaultdict(int)
    for r in resultados:
        resumen[r.estado] += 1
    logger.info("Lote de marcajes empresa=%s %s", actor.empresa_id, dict(resumen))
    return schemas.ResultadoLote(hora_servidor=ahora, resultados=resultados)

@app.get("/marcajes", response_model=list[schemas.Marcaje])
@app.get("/marcajes/", response_model=list[schemas.Marcaje], include_in_schema=False)
def read_marcajes(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    q = db.query(models.Marcaje).filter(models.Marcaje.empresa_id == _empresa_id_or_500(current_user))
    return alcance.filtrar_marcajes(q, db, current_user).order_by(models.Marcaje.timestamp_local.desc()).all()

@app.get("/marcajes/{marcaje_id}", response_model=schemas.Marcaje)
def get_marcaje(
    marcaje_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    marcaje = _get_o_404(db, models.Marcaje, marcaje_id, _empresa_id_or_500(current_user), "Marcaje")
    alcance.exigir_obra(db, current_user, marcaje.obra_id)
    return marcaje

@app.get("/trabajadores/{trabajador_id}/marcajes", response_model=list[schemas.Marcaje])
def get_marcajes_trabajador(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(usuario_empresa)
):
    empresa_id = _empresa_id_or_500(current_user)
    trabajador = _get_o_404(db, models.Trabajador, trabajador_id, empresa_id, "Trabajador")
    alcance.exigir_trabajador(db, current_user, trabajador)
    q = (
        db.query(models.Marcaje)
        .filter(models.Marcaje.trabajador_id == trabajador_id)
        .filter(models.Marcaje.empresa_id == empresa_id)
    )
    return alcance.filtrar_marcajes(q, db, current_user).order_by(models.Marcaje.timestamp_local.desc()).all()


@app.get("/planillas/resumen", response_model=schemas.PlanillaResumenResponse)
def planilla_resumen(
    fecha_inicio: datetime,
    fecha_fin: datetime,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_superadmin),
):
    if fecha_fin < fecha_inicio:
        raise HTTPException(status_code=400, detail="Rango de fechas inválido")
    return _build_planilla_resumen(db, _empresa_id_or_500(current_user), fecha_inicio, fecha_fin)

# =============================================================================
# DISPOSITIVOS (lectura: cualquier usuario; escritura y tokens: admin)
# =============================================================================

@app.get("/dispositivos", response_model=list[schemas.Dispositivo])
@app.get("/dispositivos/", response_model=list[schemas.Dispositivo], include_in_schema=False)
def get_dispositivos(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    return db.query(models.Dispositivo).filter(models.Dispositivo.empresa_id == _empresa_id_or_500(current_user)).all()

@app.get("/dispositivos/{dispositivo_id}", response_model=schemas.Dispositivo)
def get_dispositivo(
    dispositivo_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    return _get_o_404(db, models.Dispositivo, dispositivo_id, _empresa_id_or_500(current_user), "Dispositivo")

def _validar_obra_dispositivo(db: Session, obra_id, empresa_id):
    _validar_obra_empresa(db, obra_id, empresa_id)

def _respuesta_con_token(dispositivo: models.Dispositivo, token: str) -> schemas.DispositivoConToken:
    base = schemas.Dispositivo.model_validate(dispositivo).model_dump()
    return schemas.DispositivoConToken(**base, token=token)

@app.post("/dispositivos", response_model=schemas.DispositivoConToken)
@app.post("/dispositivos/", response_model=schemas.DispositivoConToken, include_in_schema=False)
def create_dispositivo(
    dispositivo: schemas.DispositivoCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    """Registra un dispositivo y devuelve su token. El token solo se muestra esta vez:
    configúralo en el teléfono (header X-Device-Token)."""
    empresa_id = _empresa_id_or_500(current_user)
    _validar_obra_dispositivo(db, dispositivo.obra_id, empresa_id)
    token = auth.generar_token_dispositivo()
    db_dispositivo = models.Dispositivo(
        **dispositivo.model_dump(),
        empresa_id=empresa_id,
        token_hash=auth.hash_token_dispositivo(token),
        activo=True,
    )
    db.add(db_dispositivo)
    db.commit()
    db.refresh(db_dispositivo)
    return _respuesta_con_token(db_dispositivo, token)

@app.post("/dispositivos/{dispositivo_id}/token", response_model=schemas.DispositivoConToken)
def rotar_token_dispositivo(
    dispositivo_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    """Genera un token nuevo para el dispositivo (el anterior deja de funcionar)."""
    db_dispositivo = _get_o_404(db, models.Dispositivo, dispositivo_id, _empresa_id_or_500(current_user), "Dispositivo")
    token = auth.generar_token_dispositivo()
    db_dispositivo.token_hash = auth.hash_token_dispositivo(token)
    db.commit()
    db.refresh(db_dispositivo)
    return _respuesta_con_token(db_dispositivo, token)

@app.put("/dispositivos/{dispositivo_id}", response_model=schemas.Dispositivo)
def update_dispositivo(
    dispositivo_id: uuid.UUID,
    dispositivo: schemas.DispositivoUpdate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    empresa_id = _empresa_id_or_500(current_user)
    db_dispositivo = _get_o_404(db, models.Dispositivo, dispositivo_id, empresa_id, "Dispositivo")
    cambios = dispositivo.model_dump(exclude_unset=True)
    if "obra_id" in cambios:
        _validar_obra_dispositivo(db, cambios["obra_id"], empresa_id)
    for field, value in cambios.items():
        setattr(db_dispositivo, field, value)
    db.commit()
    db.refresh(db_dispositivo)
    return db_dispositivo

@app.delete("/dispositivos/{dispositivo_id}")
def delete_dispositivo(
    dispositivo_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    db_dispositivo = _get_o_404(db, models.Dispositivo, dispositivo_id, _empresa_id_or_500(current_user), "Dispositivo")
    db.delete(db_dispositivo)
    db.commit()
    return {"detail": "Dispositivo deleted successfully"}

# =============================================================================
# KIOSCO (teléfono instalado en una obra, autenticado con X-Device-Token)
# =============================================================================

@app.get("/kiosco/config", response_model=schemas.KioscoConfig)
def get_kiosco_config(
    db: Session = Depends(get_db),
    dispositivo: models.Dispositivo = Depends(auth.get_dispositivo_actual),
):
    """Lo que el teléfono necesita para marcar sin conexión: su obra (con geocerca),
    los trabajadores activos de la empresa y la hora del servidor."""
    empresa = db.query(models.Empresa).filter(models.Empresa.id == dispositivo.empresa_id).first()
    if empresa is None:
        raise HTTPException(status_code=500, detail="Dispositivo sin empresa asignada")
    obra = None
    if dispositivo.obra_id is not None:
        obra = (
            db.query(models.Obra)
            .filter(models.Obra.id == dispositivo.obra_id)
            .filter(models.Obra.empresa_id == empresa.id)
            .first()
        )
    q_trab = (
        db.query(models.Trabajador)
        .filter(models.Trabajador.empresa_id == empresa.id)
        .filter(models.Trabajador.activo.is_not(False))
    )
    if obra is not None:
        # Solo los trabajadores de esta obra (y los que aún no tienen obra asignada)
        q_trab = q_trab.filter(or_(models.Trabajador.obra_id == obra.id, models.Trabajador.obra_id.is_(None)))
    trabajadores = q_trab.order_by(models.Trabajador.nombre).all()
    ahora = datetime.now(timezone.utc)
    tz = zona_horaria_empresa(empresa)
    desde = ahora.astimezone(tz).replace(tzinfo=None) - VENTANA_TURNO
    ultimos = {}
    for m in (
        db.query(models.Marcaje)
        .filter(models.Marcaje.empresa_id == empresa.id)
        .filter(models.Marcaje.timestamp_local >= desde)
        .order_by(models.Marcaje.timestamp_local.asc())
    ):
        ultimos[m.trabajador_id] = m
    items = []
    for t in trabajadores:
        ultimo = ultimos.get(t.id)
        items.append(schemas.KioscoTrabajador(
            id=t.id,
            nombre=t.nombre,
            codigo_empresa=t.codigo_empresa,
            ultimo_tipo=(ultimo.tipo or "").lower() if ultimo else None,
            ultimo_marcaje=(
                ultimo.timestamp_local.replace(tzinfo=tz).astimezone(timezone.utc) if ultimo else None
            ),
            huella_b64=(
                rostros.a_base64(t.embedding)
                if t.embedding is not None and t.embedding_modelo == rostros.MODELO else None
            ),
        ))
    dispositivo.last_sync = ahora.replace(tzinfo=None)
    db.commit()
    return schemas.KioscoConfig(
        dispositivo_id=dispositivo.id,
        dispositivo_nombre=dispositivo.nombre,
        empresa_nombre=empresa.nombre,
        empresa_codigo=empresa.slug,
        zona_horaria=empresa.zona_horaria or models.ZONA_HORARIA_DEFAULT,
        hora_servidor=ahora,
        obra=obra,
        trabajadores=items,
    )

# =============================================================================
# USUARIOS (admin gestiona usuarios de su empresa; solo superadmin crea superadmins)
# =============================================================================

def _respuesta_usuario(db: Session, u: models.UsuarioAdmin) -> schemas.UsuarioAdmin:
    base = schemas.UsuarioAdmin.model_validate(u)
    base.rol = auth.rol_de(u)
    base.obras = alcance.obras_asignadas(db, u) if auth.rol_de(u) == ROL_SUPERVISOR else []
    return base

def _roles_que_asigna(current_user: models.UsuarioAdmin) -> set[str]:
    """superadmin crea cualquier rol de su empresa; jefe solo supervisores."""
    return ROLES_EMPRESA if auth.rol_de(current_user) == ROL_SUPERADMIN else {ROL_SUPERVISOR}

def _validar_rol_asignable(rol: str | None, current_user: models.UsuarioAdmin) -> str:
    rol = (rol or "").strip().lower()
    if rol not in ROLES_EMPRESA:
        raise HTTPException(status_code=422, detail=f"Rol inválido. Opciones: {sorted(ROLES_EMPRESA)}")
    if rol not in _roles_que_asigna(current_user):
        raise HTTPException(status_code=403, detail=f"Tu rol no puede asignar el rol {rol}")
    return rol

def _exigir_puede_gestionar(current_user: models.UsuarioAdmin, objetivo: models.UsuarioAdmin):
    if auth.rol_de(objetivo) not in _roles_que_asigna(current_user):
        raise HTTPException(status_code=403, detail="No puedes modificar a un usuario con ese rol")

def _identificadores_libres(db: Session, empresa_id, usuario: str | None, email: str | None, excepto_id=None):
    def existe(columna, valor):
        q = (
            db.query(models.UsuarioAdmin)
            .filter(models.UsuarioAdmin.empresa_id == empresa_id)
            .filter(func.lower(columna) == valor)
        )
        if excepto_id is not None:
            q = q.filter(models.UsuarioAdmin.id != excepto_id)
        return q.first() is not None

    if usuario and existe(models.UsuarioAdmin.usuario, usuario):
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese nombre de usuario")
    if email and existe(models.UsuarioAdmin.email, email):
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese email")

def _normalizar_email(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    return email or None

def _asignar_obras(db: Session, usuario: models.UsuarioAdmin, obras: list[int], empresa_id):
    db.query(models.SupervisorObra).filter(models.SupervisorObra.usuario_id == usuario.id).delete()
    for obra_id in sorted(set(obras)):
        _get_o_404(db, models.Obra, obra_id, empresa_id, "Obra")
        db.add(models.SupervisorObra(usuario_id=usuario.id, obra_id=obra_id))

@app.post("/usuarios_admin", response_model=schemas.UsuarioAdmin)
@app.post("/usuarios_admin/", response_model=schemas.UsuarioAdmin, include_in_schema=False)
def create_usuario_admin(
    usuario: schemas.UsuarioAdminCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    empresa_id = _empresa_id_or_500(current_user)
    rol = _validar_rol_asignable(usuario.rol, current_user)
    email = _normalizar_email(usuario.email)
    _identificadores_libres(db, empresa_id, usuario.usuario, email)
    db_usuario = models.UsuarioAdmin(
        id=uuid.uuid4(),
        nombre=usuario.nombre.strip(),
        usuario=usuario.usuario,
        email=email,
        rol=rol,
        activo=True,
        hash_password=auth.get_password_hash(usuario.password),
        empresa_id=empresa_id,
    )
    db.add(db_usuario)
    db.flush()
    if rol == ROL_SUPERVISOR:
        _asignar_obras(db, db_usuario, usuario.obras, empresa_id)
    db.commit()
    db.refresh(db_usuario)
    return _respuesta_usuario(db, db_usuario)

@app.get("/usuarios_admin", response_model=list[schemas.UsuarioAdmin])
@app.get("/usuarios_admin/", response_model=list[schemas.UsuarioAdmin], include_in_schema=False)
def read_usuarios_admin(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion),
):
    usuarios = (
        db.query(models.UsuarioAdmin)
        .filter(models.UsuarioAdmin.empresa_id == _empresa_id_or_500(current_user))
        .order_by(models.UsuarioAdmin.nombre)
        .all()
    )
    return [_respuesta_usuario(db, u) for u in usuarios]

@app.get("/usuarios_admin/{usuario_id}", response_model=schemas.UsuarioAdmin)
def read_usuario_admin(
    usuario_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion),
):
    u = _get_o_404(db, models.UsuarioAdmin, usuario_id, _empresa_id_or_500(current_user), "Usuario")
    return _respuesta_usuario(db, u)

@app.put("/usuarios_admin/{usuario_id}", response_model=schemas.UsuarioAdmin)
def update_usuario_admin(
    usuario_id: uuid.UUID,
    usuario: schemas.UsuarioAdminUpdate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    empresa_id = _empresa_id_or_500(current_user)
    db_usuario = _get_o_404(db, models.UsuarioAdmin, usuario_id, empresa_id, "Usuario")
    _exigir_puede_gestionar(current_user, db_usuario)
    cambios = usuario.model_dump(exclude_unset=True)
    if db_usuario.id == current_user.id and (cambios.get("activo") is False or "rol" in cambios):
        raise HTTPException(status_code=400, detail="No puedes cambiar tu propio rol ni desactivarte")
    if "rol" in cambios:
        db_usuario.rol = _validar_rol_asignable(cambios["rol"], current_user)
    if "usuario" in cambios and cambios["usuario"]:
        _identificadores_libres(db, empresa_id, cambios["usuario"], None, excepto_id=db_usuario.id)
        db_usuario.usuario = cambios["usuario"]
    if "email" in cambios:
        email = _normalizar_email(cambios["email"])
        _identificadores_libres(db, empresa_id, None, email, excepto_id=db_usuario.id)
        db_usuario.email = email
    if cambios.get("nombre"):
        db_usuario.nombre = cambios["nombre"].strip()
    if cambios.get("password"):
        db_usuario.hash_password = auth.get_password_hash(cambios["password"])
    if "activo" in cambios and cambios["activo"] is not None:
        db_usuario.activo = cambios["activo"]
    if auth.rol_de(db_usuario) == ROL_SUPERVISOR:
        if cambios.get("obras") is not None:
            _asignar_obras(db, db_usuario, cambios["obras"], empresa_id)
    else:
        db.query(models.SupervisorObra).filter(models.SupervisorObra.usuario_id == db_usuario.id).delete()
    db.commit()
    db.refresh(db_usuario)
    return _respuesta_usuario(db, db_usuario)

@app.delete("/usuarios_admin/{usuario_id}")
def delete_usuario_admin(
    usuario_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(gestion)
):
    db_usuario = _get_o_404(db, models.UsuarioAdmin, usuario_id, _empresa_id_or_500(current_user), "Usuario")
    if db_usuario.id == current_user.id:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propio usuario")
    _exigir_puede_gestionar(current_user, db_usuario)
    db.query(models.SupervisorObra).filter(models.SupervisorObra.usuario_id == db_usuario.id).delete()
    db.query(models.Marcaje).filter(models.Marcaje.registrado_por == db_usuario.id).update(
        {models.Marcaje.registrado_por: None}
    )
    db.delete(db_usuario)
    db.commit()
    return {"ok": True}

# =============================================================================
# PLATAFORMA (dueño de LaborLog): alta de empresas. No ve datos internos.
# =============================================================================

def _empresa_plataforma(db: Session, e: models.Empresa) -> schemas.EmpresaPlataforma:
    def contar(modelo):
        return db.query(func.count()).select_from(modelo).filter(modelo.empresa_id == e.id).scalar() or 0

    return schemas.EmpresaPlataforma(
        id=e.id, nombre=e.nombre, codigo=e.slug, activa=e.activa is not False,
        zona_horaria=e.zona_horaria or models.ZONA_HORARIA_DEFAULT, created_at=e.created_at,
        usuarios=contar(models.UsuarioAdmin), trabajadores=contar(models.Trabajador),
        obras=contar(models.Obra), dispositivos=contar(models.Dispositivo),
    )

def _codigo_libre(db: Session, codigo: str, excepto_id=None):
    if codigo == CODIGO_PLATAFORMA:
        raise HTTPException(status_code=422, detail="Ese código está reservado")
    q = db.query(models.Empresa).filter(func.lower(models.Empresa.slug) == codigo)
    if excepto_id is not None:
        q = q.filter(models.Empresa.id != excepto_id)
    if q.first():
        raise HTTPException(status_code=409, detail="Ya existe una empresa con ese código")

@app.get("/plataforma/empresas", response_model=list[schemas.EmpresaPlataforma])
def plataforma_empresas(db: Session = Depends(get_db), _u: models.UsuarioAdmin = Depends(solo_plataforma)):
    return [_empresa_plataforma(db, e) for e in db.query(models.Empresa).order_by(models.Empresa.nombre).all()]

@app.post("/plataforma/empresas", response_model=schemas.EmpresaPlataforma)
def plataforma_crear_empresa(
    datos: schemas.EmpresaNueva,
    db: Session = Depends(get_db),
    _u: models.UsuarioAdmin = Depends(solo_plataforma),
):
    """Crea una empresa y su primer superadmin (dueño/RRHH), que luego crea a los demás."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(datos.zona_horaria)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="Zona horaria inválida (formato IANA, ej. America/Guatemala)")
    _codigo_libre(db, datos.codigo)
    empresa = models.Empresa(
        id=uuid.uuid4(), nombre=datos.nombre.strip(), slug=datos.codigo, activa=True,
        zona_horaria=datos.zona_horaria, created_at=datetime.now(),
    )
    db.add(empresa)
    db.flush()
    db.add(models.UsuarioAdmin(
        id=uuid.uuid4(), empresa_id=empresa.id, nombre=datos.superadmin.nombre.strip(),
        usuario=datos.superadmin.usuario, email=_normalizar_email(datos.superadmin.email),
        rol=ROL_SUPERADMIN, activo=True, hash_password=auth.get_password_hash(datos.superadmin.password),
    ))
    db.commit()
    logger.info("Empresa creada %s (%s)", empresa.nombre, empresa.slug)
    return _empresa_plataforma(db, empresa)

@app.put("/plataforma/empresas/{empresa_id}", response_model=schemas.EmpresaPlataforma)
def plataforma_editar_empresa(
    empresa_id: uuid.UUID,
    datos: schemas.EmpresaPlataformaUpdate,
    db: Session = Depends(get_db),
    _u: models.UsuarioAdmin = Depends(solo_plataforma),
):
    """Cambia nombre o código, o activa/desactiva la empresa (desactivada: nadie entra
    y sus teléfonos no pueden marcar)."""
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    if empresa is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    cambios = datos.model_dump(exclude_unset=True)
    if cambios.get("codigo"):
        _codigo_libre(db, cambios["codigo"], excepto_id=empresa.id)
        empresa.slug = cambios["codigo"]
    if cambios.get("nombre"):
        empresa.nombre = cambios["nombre"].strip()
    if cambios.get("activa") is not None:
        empresa.activa = cambios["activa"]
    db.commit()
    db.refresh(empresa)
    return _empresa_plataforma(db, empresa)

# =============================================================================
# FRONTEND (SPA) - solo si existe el build de Flutter Web
# =============================================================================

_SEGMENTOS_API = {
    "api", "docs", "redoc", "openapi.json", "login", "me", "empresas", "trabajadores",
    "obras", "dispositivos", "marcajes", "usuarios_admin", "planillas", "kiosco", "plataforma",
}

@app.get("/", include_in_schema=False)
async def read_index():
    index_path = os.path.join(static_path, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Frontend not found")

@app.get("/{full_path:path}", include_in_schema=False)
async def catch_all(full_path: str):
    if full_path.split("/", 1)[0] in _SEGMENTOS_API:
        raise HTTPException(status_code=404, detail="API endpoint not found")

    # Archivos del build (main.dart.js, assets/, icons/, canvaskit/, etc.)
    archivo = os.path.normpath(os.path.join(static_path, full_path))
    if archivo.startswith(os.path.normpath(static_path)) and os.path.isfile(archivo):
        return FileResponse(archivo)

    # Rutas de la SPA: servir index.html
    index_path = os.path.join(static_path, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Frontend not found")
