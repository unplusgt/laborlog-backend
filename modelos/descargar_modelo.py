"""Descarga el modelo de reconocimiento facial SFace (OpenCV Zoo, licencia Apache 2.0).

El archivo (~37 MB) no se guarda en git. Se descarga al construir la imagen Docker o a
mano para desarrollo:

    python laborlog_backend/modelos/descargar_modelo.py

Se verifica la suma SHA-256 publicada por OpenCV; si no coincide, se borra y falla.
"""
import hashlib
import os
import sys
import urllib.request

NOMBRE = "face_recognition_sface_2021dec.onnx"
URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
    + NOMBRE
)
SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
TAMANO = 38696353
DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), NOMBRE)


def _sha256(ruta: str) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def main() -> int:
    if os.path.exists(DESTINO) and _sha256(DESTINO) == SHA256:
        print(f"Modelo ya presente: {DESTINO}")
        return 0
    print(f"Descargando {URL}")
    temporal = DESTINO + ".tmp"
    urllib.request.urlretrieve(URL, temporal)
    if os.path.getsize(temporal) != TAMANO or _sha256(temporal) != SHA256:
        os.remove(temporal)
        print("ERROR: el archivo descargado no coincide con la suma SHA-256 esperada", file=sys.stderr)
        return 1
    os.replace(temporal, DESTINO)
    print(f"Modelo verificado: {DESTINO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
