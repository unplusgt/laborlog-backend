"""Almacenamiento de fotos (rostro de registro y evidencia de cada marcaje).

Se guardan como archivos en FOTOS_DIR (en Docker, un volumen). En la BD solo queda la
ruta relativa. Contienen datos biométricos: el directorio no debe ser público.
"""
import os
import uuid
from datetime import datetime

FOTOS_DIR = os.getenv(
    "FOTOS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fotos"),
)


def _ruta_absoluta(relativa: str) -> str:
    base = os.path.realpath(FOTOS_DIR)
    ruta = os.path.realpath(os.path.join(base, relativa))
    if not ruta.startswith(base + os.sep):
        raise ValueError("Ruta de foto inválida")
    return ruta


def guardar(empresa_id, tipo: str, entidad_id, datos: bytes, cuando: datetime | None = None) -> str:
    """Guarda la foto y devuelve su ruta relativa (la que va en la BD)."""
    cuando = cuando or datetime.now()
    relativa = os.path.join(
        str(uuid.UUID(str(empresa_id))), tipo, f"{cuando:%Y}", f"{cuando:%m}",
        f"{uuid.UUID(str(entidad_id))}.jpg",
    )
    ruta = _ruta_absoluta(relativa)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    temporal = ruta + ".tmp"
    with open(temporal, "wb") as f:
        f.write(datos)
    os.replace(temporal, ruta)
    return relativa.replace(os.sep, "/")


def leer(relativa: str | None) -> bytes | None:
    if not relativa:
        return None
    try:
        with open(_ruta_absoluta(relativa), "rb") as f:
            return f.read()
    except (FileNotFoundError, ValueError):
        return None


def borrar(relativa: str | None) -> None:
    if not relativa:
        return
    try:
        os.remove(_ruta_absoluta(relativa))
    except (FileNotFoundError, ValueError):
        pass
