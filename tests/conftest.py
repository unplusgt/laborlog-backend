"""Fixtures de pruebas: usan una BD SQLite temporal, no la BD real de Postgres."""
import os
import tempfile
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

_tmpdir = tempfile.mkdtemp(prefix="laborlog_tests_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_tmpdir, 'test.db')}"
os.environ["SECRET_KEY"] = "clave-de-pruebas-" + "x" * 40
os.environ["CORS_ORIGINS"] = "http://localhost:8080"
os.environ["FOTOS_DIR"] = os.path.join(_tmpdir, "fotos")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from laborlog_backend import auth, models, rostros  # noqa: E402
from laborlog_backend.database import Base, SessionLocal, engine  # noqa: E402
from laborlog_backend.main import app  # noqa: E402

TZ = ZoneInfo("America/Guatemala")
PASSWORD = "clave-segura-123"
# Obra de prueba (coordenadas de la obra BARI del backup) con radio de 50 m
OBRA_LAT, OBRA_LNG = 14.538364, -90.478339


def ahora_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


class EmbebedorFalso:
    """Sustituye al modelo SFace en las pruebas: la "foto" es b'ÿØ' + nombre de
    la persona, y cada persona tiene una huella fija distinta."""

    def huella(self, jpeg: bytes):
        import numpy as np
        semilla = sum(jpeg[2:]) % (2**32)
        return rostros.normalizar(np.random.RandomState(semilla).randn(128))


@pytest.fixture(autouse=True)
def _modelo_facial_falso():
    rostros.fijar_embebedor(EmbebedorFalso())
    yield
    rostros.fijar_embebedor(EmbebedorFalso())


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    return TestClient(app)


@pytest.fixture()
def datos(db):
    """Empresa con admin, supervisor, obra, trabajador y dispositivo (con token)."""
    empresa = models.Empresa(id=uuid.uuid4(), nombre="Constructora Test", slug="test", zona_horaria="America/Guatemala")
    otra = models.Empresa(id=uuid.uuid4(), nombre="Otra", slug="otra", zona_horaria="America/Guatemala")
    db.add_all([empresa, otra])
    db.flush()

    def usuario(email, rol, emp=empresa):
        u = models.UsuarioAdmin(
            id=uuid.uuid4(), nombre=email.split("@")[0], usuario=email.split("@")[0], email=email, rol=rol,
            activo=True, hash_password=auth.get_password_hash(PASSWORD), empresa_id=emp.id if emp else None,
        )
        db.add(u)
        return u

    # "admin" de las pruebas es el JEFE (todo menos planilla); "root" es el superadmin
    admin = usuario("admin@test.com", "jefe")
    supervisor = usuario("super@test.com", "supervisor")
    superadmin = usuario("root@test.com", "superadmin")
    admin_otra = usuario("admin@otra.com", "superadmin", otra)
    plataforma = usuario("duenio@laborlog.app", "plataforma", None)

    obra = models.Obra(id=1, nombre="BARI", lat=str(OBRA_LAT), lng=str(OBRA_LNG), radio_metros=50, empresa_id=empresa.id)
    obra_sin_gps = models.Obra(id=2, nombre="Sin GPS", empresa_id=empresa.id)
    trabajador = models.Trabajador(id=uuid.uuid4(), nombre="Juan Perez", activo=True, empresa_id=empresa.id, obra_id=1)
    inactivo = models.Trabajador(id=uuid.uuid4(), nombre="Ex Trabajador", activo=False, empresa_id=empresa.id, obra_id=1)
    trabajador_otra = models.Trabajador(id=uuid.uuid4(), nombre="De otra empresa", activo=True, empresa_id=otra.id)

    device_token = auth.generar_token_dispositivo()
    dispositivo = models.Dispositivo(
        id=uuid.uuid4(), nombre="Samsung obra BARI", empresa_id=empresa.id,
        token_hash=auth.hash_token_dispositivo(device_token), activo=True,
    )
    db.add_all([obra, obra_sin_gps, trabajador, inactivo, trabajador_otra, dispositivo])
    db.flush()
    # El supervisor tiene asignadas las obras 1 y 2
    db.add_all([models.SupervisorObra(usuario_id=supervisor.id, obra_id=1),
                models.SupervisorObra(usuario_id=supervisor.id, obra_id=2)])
    db.commit()

    return {
        "empresa": empresa, "otra": otra, "admin": admin, "supervisor": supervisor,
        "superadmin": superadmin, "admin_otra": admin_otra, "plataforma": plataforma, "obra": obra,
        "obra_sin_gps": obra_sin_gps, "trabajador": trabajador, "inactivo": inactivo,
        "trabajador_otra": trabajador_otra, "dispositivo": dispositivo, "device_token": device_token,
    }


def empresa_de(email: str) -> str:
    if email.endswith("@laborlog.app"):
        return "plataforma"
    return "otra" if email.endswith("@otra.com") else "test"


def headers_de(client, email, empresa=None):
    r = client.post("/login", data={"username": email, "password": PASSWORD, "empresa": empresa or empresa_de(email)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def payload_marcaje(datos, tipo="entrada", lat=OBRA_LAT, lng=OBRA_LNG, ts=None, **extra):
    data = {
        "trabajador_id": str(datos["trabajador"].id),
        "obra_id": datos["obra"].id,
        "tipo": tipo,
        "timestamp_local": (ts or ahora_local()).isoformat(),
        "origen": str(datos["dispositivo"].id),
        "lat": f"{lat:.6f}",
        "lng": f"{lng:.6f}",
    }
    data.update(extra)
    return data
