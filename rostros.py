"""Reconocimiento facial en el servidor (verificación 1:1).

El teléfono identifica al trabajador sin conexión con el mismo modelo (SFace). Cuando
el marcaje llega con su foto, el servidor vuelve a calcular la huella facial de esa
foto y la compara con la registrada del trabajador. Así, una app alterada que diga
"reconocí a Juan" con la cara de otra persona queda marcada como conflicto.

La foto que manda el teléfono ya viene alineada (ojos, nariz y boca en posiciones
fijas, como pide SFace), así que aquí solo se redimensiona a 112x112.
"""
import base64
import logging
import os
import threading

import numpy as np

logger = logging.getLogger("laborlog_backend.rostros")

MODELO = "sface-2021dec"
DIMENSION = 128
# Umbral de similitud coseno recomendado por OpenCV para SFace
UMBRAL_SIMILITUD = 0.363
# Al registrar: la huella enviada por el teléfono debe parecerse a la de su foto
UMBRAL_REGISTRO = 0.6
MAX_FOTO_BYTES = 150_000

_RUTA_MODELO = os.getenv(
    "SFACE_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "modelos",
                 "face_recognition_sface_2021dec.onnx"),
)


class RostroInvalido(ValueError):
    pass


# ----------------------------------------------------------------- huellas
def normalizar(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n == 0 or not np.isfinite(n):
        raise RostroInvalido("Huella facial inválida")
    return v / n


def a_bytes(v) -> bytes:
    return normalizar(v).astype("<f4").tobytes()


def de_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<f4")


def de_base64(texto: str) -> np.ndarray:
    try:
        crudo = base64.b64decode(texto, validate=True)
    except Exception:
        raise RostroInvalido("Huella facial mal codificada")
    if len(crudo) != DIMENSION * 4:
        raise RostroInvalido(f"La huella facial debe tener {DIMENSION} valores")
    return normalizar(np.frombuffer(crudo, dtype="<f4"))


def a_base64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def similitud(a, b) -> float:
    return float(np.dot(normalizar(a), normalizar(b)))


def foto_de_base64(texto: str) -> bytes:
    try:
        datos = base64.b64decode(texto, validate=True)
    except Exception:
        raise RostroInvalido("Foto mal codificada")
    if len(datos) > MAX_FOTO_BYTES:
        raise RostroInvalido("La foto es demasiado grande")
    if not datos.startswith(b"\xff\xd8"):
        raise RostroInvalido("La foto debe ser JPEG")
    return datos


# ----------------------------------------------------------------- modelo
class Embebedor:
    """Calcula la huella facial de una foto alineada con SFace (OpenCV)."""

    def __init__(self, ruta_modelo: str):
        import cv2  # import diferido: solo si hay modelo

        self._cv2 = cv2
        self._rec = cv2.FaceRecognizerSF.create(ruta_modelo, "")
        self._lock = threading.Lock()

    def huella(self, jpeg: bytes) -> np.ndarray:
        cv2 = self._cv2
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RostroInvalido("No se pudo leer la foto")
        if img.shape[:2] != (112, 112):
            img = cv2.resize(img, (112, 112), interpolation=cv2.INTER_AREA)
        with self._lock:
            return normalizar(self._rec.feature(img))


_embebedor = None
_embebedor_cargado = False
_carga_lock = threading.Lock()


def embebedor():
    """El modelo cargado, o None si no está disponible (se registra un aviso y el
    servidor acepta los marcajes sin verificar la cara)."""
    global _embebedor, _embebedor_cargado
    if _embebedor_cargado:
        return _embebedor
    with _carga_lock:
        if not _embebedor_cargado:
            try:
                if not os.path.exists(_RUTA_MODELO):
                    raise FileNotFoundError(_RUTA_MODELO)
                _embebedor = Embebedor(_RUTA_MODELO)
                logger.info("Modelo facial cargado: %s", _RUTA_MODELO)
            except Exception as exc:  # opencv o modelo ausentes
                logger.warning("Verificación facial desactivada: %s", exc)
                _embebedor = None
            _embebedor_cargado = True
    return _embebedor


def fijar_embebedor(e) -> None:
    """Para pruebas."""
    global _embebedor, _embebedor_cargado
    _embebedor, _embebedor_cargado = e, True
