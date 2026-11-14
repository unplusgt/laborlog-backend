import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Cargar variables de entorno desde .env (si existe)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "Falta la variable de entorno DATABASE_URL. Copia .env.example a .env y complétala."
    )

# El proyecto instala psycopg2-binary. SQLAlchemy 2.1 usa por defecto el driver
# psycopg (v3) para "postgresql://", así que se fija el driver explícitamente.
for _prefijo in ("postgresql://", "postgres://"):
    if DATABASE_URL.startswith(_prefijo):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_prefijo):]

_connect_args = {}
if DATABASE_URL.startswith("postgresql"):
    _connect_args = {"options": "-c client_encoding=utf8"}
elif DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependencia de FastAPI: una sesión de BD por request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# El esquema se gestiona con Alembic (laborlog_backend/migrations).
# Para crear/actualizar la BD: `alembic upgrade head` desde la raíz del proyecto.
