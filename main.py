import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, models, schemas
from .auth import ROLES_ADMIN, ROLES_VALIDOS, ROL_SUPERADMIN
from .database import get_db
from .marcajes import registrar_marcaje

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
solo_admin = auth.require_role(ROLES_ADMIN)


def _empresa_id_or_500(current_user: models.UsuarioAdmin):
    if not current_user.empresa_id:
        raise HTTPException(status_code=500, detail="Usuario sin empresa asignada")
    return current_user.empresa_id


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
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = auth.authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth.create_access_token(data={"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/me")
def read_users_me(current_user: models.UsuarioAdmin = Depends(auth.get_current_user)):
    return {
        "user": current_user.email,
        "id": str(current_user.id),
        "nombre": current_user.nombre,
        "rol": auth.rol_de(current_user),
        "empresa_id": str(current_user.empresa_id) if current_user.empresa_id else None,
    }


@app.get("/empresas/me", response_model=schemas.Empresa)
def get_empresa_actual(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user),
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
    current_user: models.UsuarioAdmin = Depends(solo_admin),
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
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return db.query(models.Trabajador).filter(models.Trabajador.empresa_id == _empresa_id_or_500(current_user)).all()

@app.get("/trabajadores/{trabajador_id}", response_model=schemas.Trabajador)
def get_trabajador(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")

@app.post("/trabajadores", response_model=schemas.Trabajador)
@app.post("/trabajadores/", response_model=schemas.Trabajador, include_in_schema=False)
def create_trabajador(
    trabajador: schemas.TrabajadorCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_trabajador = models.Trabajador(
        **trabajador.model_dump(),
        empresa_id=_empresa_id_or_500(current_user),
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
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_trabajador = _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")
    for field, value in trabajador.model_dump(exclude_unset=True).items():
        setattr(db_trabajador, field, value)
    db.commit()
    db.refresh(db_trabajador)
    return db_trabajador

@app.delete("/trabajadores/{trabajador_id}")
def delete_trabajador(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_trabajador = _get_o_404(db, models.Trabajador, trabajador_id, _empresa_id_or_500(current_user), "Trabajador")
    db.delete(db_trabajador)
    db.commit()
    return {"detail": "Trabajador deleted successfully"}

# =============================================================================
# OBRAS (lectura: cualquier usuario; escritura: admin)
# =============================================================================

@app.get("/obras", response_model=list[schemas.Obra])
@app.get("/obras/", response_model=list[schemas.Obra], include_in_schema=False)
def get_obras(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return db.query(models.Obra).filter(models.Obra.empresa_id == _empresa_id_or_500(current_user)).all()

@app.get("/obras/{obra_id}", response_model=schemas.Obra)
def get_obra(
    obra_id: int,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return _get_o_404(db, models.Obra, obra_id, _empresa_id_or_500(current_user), "Obra")

@app.post("/obras", response_model=schemas.Obra)
@app.post("/obras/", response_model=schemas.Obra, include_in_schema=False)
def create_obra(
    obra: schemas.ObraCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
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
    current_user: models.UsuarioAdmin = Depends(solo_admin)
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
    current_user: models.UsuarioAdmin = Depends(solo_admin)
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

@app.get("/marcajes", response_model=list[schemas.Marcaje])
@app.get("/marcajes/", response_model=list[schemas.Marcaje], include_in_schema=False)
def read_marcajes(
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return (
        db.query(models.Marcaje)
        .filter(models.Marcaje.empresa_id == _empresa_id_or_500(current_user))
        .order_by(models.Marcaje.timestamp_local.desc())
        .all()
    )

@app.get("/marcajes/{marcaje_id}", response_model=schemas.Marcaje)
def get_marcaje(
    marcaje_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return _get_o_404(db, models.Marcaje, marcaje_id, _empresa_id_or_500(current_user), "Marcaje")

@app.get("/trabajadores/{trabajador_id}/marcajes", response_model=list[schemas.Marcaje])
def get_marcajes_trabajador(
    trabajador_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return (
        db.query(models.Marcaje)
        .filter(models.Marcaje.trabajador_id == trabajador_id)
        .filter(models.Marcaje.empresa_id == _empresa_id_or_500(current_user))
        .order_by(models.Marcaje.timestamp_local.desc())
        .all()
    )


@app.get("/planillas/resumen", response_model=schemas.PlanillaResumenResponse)
def planilla_resumen(
    fecha_inicio: datetime,
    fecha_fin: datetime,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin),
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
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return db.query(models.Dispositivo).filter(models.Dispositivo.empresa_id == _empresa_id_or_500(current_user)).all()

@app.get("/dispositivos/{dispositivo_id}", response_model=schemas.Dispositivo)
def get_dispositivo(
    dispositivo_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(auth.get_current_user)
):
    return _get_o_404(db, models.Dispositivo, dispositivo_id, _empresa_id_or_500(current_user), "Dispositivo")

def _respuesta_con_token(dispositivo: models.Dispositivo, token: str) -> schemas.DispositivoConToken:
    base = schemas.Dispositivo.model_validate(dispositivo).model_dump()
    return schemas.DispositivoConToken(**base, token=token)

@app.post("/dispositivos", response_model=schemas.DispositivoConToken)
@app.post("/dispositivos/", response_model=schemas.DispositivoConToken, include_in_schema=False)
def create_dispositivo(
    dispositivo: schemas.DispositivoCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    """Registra un dispositivo y devuelve su token. El token solo se muestra esta vez:
    configúralo en el teléfono (header X-Device-Token)."""
    token = auth.generar_token_dispositivo()
    db_dispositivo = models.Dispositivo(
        **dispositivo.model_dump(),
        empresa_id=_empresa_id_or_500(current_user),
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
    current_user: models.UsuarioAdmin = Depends(solo_admin)
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
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_dispositivo = _get_o_404(db, models.Dispositivo, dispositivo_id, _empresa_id_or_500(current_user), "Dispositivo")
    for field, value in dispositivo.model_dump(exclude_unset=True).items():
        setattr(db_dispositivo, field, value)
    db.commit()
    db.refresh(db_dispositivo)
    return db_dispositivo

@app.delete("/dispositivos/{dispositivo_id}")
def delete_dispositivo(
    dispositivo_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_dispositivo = _get_o_404(db, models.Dispositivo, dispositivo_id, _empresa_id_or_500(current_user), "Dispositivo")
    db.delete(db_dispositivo)
    db.commit()
    return {"detail": "Dispositivo deleted successfully"}

# =============================================================================
# USUARIOS (admin gestiona usuarios de su empresa; solo superadmin crea superadmins)
# =============================================================================

def _validar_rol_asignable(rol: str | None, current_user: models.UsuarioAdmin) -> str:
    rol = (rol or "").strip().lower()
    if rol not in ROLES_VALIDOS:
        raise HTTPException(status_code=422, detail=f"Rol inválido. Opciones: {sorted(ROLES_VALIDOS)}")
    if rol == ROL_SUPERADMIN and auth.rol_de(current_user) != ROL_SUPERADMIN:
        raise HTTPException(status_code=403, detail="Solo un superadmin puede asignar el rol superadmin")
    return rol

def _email_libre(db: Session, email: str, excepto_id=None):
    q = db.query(models.UsuarioAdmin).filter(models.UsuarioAdmin.email == email)
    if excepto_id is not None:
        q = q.filter(models.UsuarioAdmin.id != excepto_id)
    if q.first():
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese email")

@app.post("/usuarios_admin", response_model=schemas.UsuarioAdmin)
@app.post("/usuarios_admin/", response_model=schemas.UsuarioAdmin, include_in_schema=False)
def create_usuario_admin(
    usuario: schemas.UsuarioAdminCreate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    rol = _validar_rol_asignable(usuario.rol, current_user)
    email = usuario.email.strip().lower()
    _email_libre(db, email)
    db_usuario = models.UsuarioAdmin(
        nombre=usuario.nombre,
        email=email,
        rol=rol,
        hash_password=auth.get_password_hash(usuario.password),
        empresa_id=_empresa_id_or_500(current_user),
    )
    db.add(db_usuario)
    db.commit()
    db.refresh(db_usuario)
    return db_usuario

@app.get("/usuarios_admin", response_model=list[schemas.UsuarioAdmin])
@app.get("/usuarios_admin/", response_model=list[schemas.UsuarioAdmin], include_in_schema=False)
def read_usuarios_admin(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin),
):
    return (
        db.query(models.UsuarioAdmin)
        .filter(models.UsuarioAdmin.empresa_id == _empresa_id_or_500(current_user))
        .offset(skip)
        .limit(limit)
        .all()
    )

@app.get("/usuarios_admin/{usuario_id}", response_model=schemas.UsuarioAdmin)
def read_usuario_admin(
    usuario_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin),
):
    return _get_o_404(db, models.UsuarioAdmin, usuario_id, _empresa_id_or_500(current_user), "UsuarioAdmin")

@app.put("/usuarios_admin/{usuario_id}", response_model=schemas.UsuarioAdmin)
def update_usuario_admin(
    usuario_id: uuid.UUID,
    usuario: schemas.UsuarioAdminUpdate,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_usuario = _get_o_404(db, models.UsuarioAdmin, usuario_id, _empresa_id_or_500(current_user), "UsuarioAdmin")
    if auth.rol_de(db_usuario) == ROL_SUPERADMIN and auth.rol_de(current_user) != ROL_SUPERADMIN:
        raise HTTPException(status_code=403, detail="Solo un superadmin puede editar a otro superadmin")
    cambios = usuario.model_dump(exclude_unset=True)
    if "rol" in cambios:
        db_usuario.rol = _validar_rol_asignable(cambios["rol"], current_user)
    if cambios.get("email"):
        email = cambios["email"].strip().lower()
        _email_libre(db, email, excepto_id=db_usuario.id)
        db_usuario.email = email
    if cambios.get("nombre"):
        db_usuario.nombre = cambios["nombre"]
    if cambios.get("password"):
        db_usuario.hash_password = auth.get_password_hash(cambios["password"])
    db.commit()
    db.refresh(db_usuario)
    return db_usuario

@app.delete("/usuarios_admin/{usuario_id}")
def delete_usuario_admin(
    usuario_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.UsuarioAdmin = Depends(solo_admin)
):
    db_usuario = _get_o_404(db, models.UsuarioAdmin, usuario_id, _empresa_id_or_500(current_user), "UsuarioAdmin")
    if db_usuario.id == current_user.id:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propio usuario")
    if auth.rol_de(db_usuario) == ROL_SUPERADMIN and auth.rol_de(current_user) != ROL_SUPERADMIN:
        raise HTTPException(status_code=403, detail="Solo un superadmin puede eliminar a otro superadmin")
    db.delete(db_usuario)
    db.commit()
    return {"ok": True}

# =============================================================================
# FRONTEND (SPA) - solo si existe el build de Flutter Web
# =============================================================================

_SEGMENTOS_API = {
    "api", "docs", "redoc", "openapi.json", "login", "me", "empresas", "trabajadores",
    "obras", "dispositivos", "marcajes", "usuarios_admin", "planillas",
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
