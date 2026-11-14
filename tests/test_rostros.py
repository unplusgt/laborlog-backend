"""Fase 2: registro de rostros, verificación facial en el servidor y fotos de marcajes."""
import base64
import os
import uuid

import pytest

from laborlog_backend import fotos, models, rostros

from .conftest import EmbebedorFalso, ahora_local, headers_de, payload_marcaje


def _foto(persona: str) -> bytes:
    return b"\xff\xd8" + persona.encode()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _huella_b64(persona: str) -> str:
    return _b64(rostros.a_bytes(EmbebedorFalso().huella(_foto(persona))))


def _registro(persona: str, **extra):
    data = {
        "huella_b64": _huella_b64(persona),
        "foto_jpeg_b64": _b64(_foto(persona)),
        "consentimiento": True,
        "modelo": rostros.MODELO,
    }
    data.update(extra)
    return data


def _registrar(client, datos, trabajador_key="trabajador", persona="juan", usuario="super@test.com"):
    h = headers_de(client, usuario)
    return client.put(f"/trabajadores/{datos[trabajador_key].id}/rostro", json=_registro(persona), headers=h)


def _dev(datos):
    return {"X-Device-Token": datos["device_token"]}


def _lote(client, datos, items):
    r = client.post("/marcajes/lote", json={"enviado_en": ahora_local().isoformat(), "marcajes": items},
                    headers=_dev(datos))
    assert r.status_code == 200, r.text
    return r.json()["resultados"]


def _item_facial(datos, persona, tipo="entrada", **extra):
    data = payload_marcaje(datos, tipo=tipo, metodo="rostro", similitud=0.8, prueba_vida="parpadeo",
                           foto_jpeg_b64=_b64(_foto(persona)), id=str(uuid.uuid4()))
    data.pop("origen")
    data.update(extra)
    return data


# --- Registro de rostro ----------------------------------------------------------

def test_supervisor_registra_rostro(client, datos, db):
    r = _registrar(client, datos)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rostro_registrado"] is True
    assert "embedding" not in body and "huella_b64" not in body
    db.refresh(datos["trabajador"])
    t = datos["trabajador"]
    assert t.embedding_modelo == rostros.MODELO
    assert t.consentimiento_biometrico_en is not None
    assert fotos.leer(t.foto_registro_path) == _foto("juan")


def test_registro_sin_consentimiento(client, datos):
    h = headers_de(client, "super@test.com")
    r = client.put(f"/trabajadores/{datos['trabajador'].id}/rostro",
                   json=_registro("juan", consentimiento=False), headers=h)
    assert r.status_code == 422
    assert "autorización" in r.json()["error"]


def test_registro_foto_que_no_corresponde_a_la_huella(client, datos):
    h = headers_de(client, "super@test.com")
    data = _registro("juan", foto_jpeg_b64=_b64(_foto("otra persona")))
    r = client.put(f"/trabajadores/{datos['trabajador'].id}/rostro", json=data, headers=h)
    assert r.status_code == 422
    assert "no corresponde" in r.json()["error"]


def test_registro_rechaza_rostro_de_otro_trabajador(client, datos, db):
    otro = models.Trabajador(id=uuid.uuid4(), nombre="Pedro", activo=True, empresa_id=datos["empresa"].id)
    db.add(otro)
    db.commit()
    assert _registrar(client, datos, persona="juan").status_code == 200
    h = headers_de(client, "super@test.com")
    r = client.put(f"/trabajadores/{otro.id}/rostro", json=_registro("juan"), headers=h)
    assert r.status_code == 409
    assert "Juan Perez" in r.json()["error"]


def test_registro_valida_datos(client, datos):
    h = headers_de(client, "super@test.com")
    url = f"/trabajadores/{datos['trabajador'].id}/rostro"
    assert client.put(url, json=_registro("juan", huella_b64=_b64(b"corto")), headers=h).status_code == 422
    assert client.put(url, json=_registro("juan", foto_jpeg_b64=_b64(b"PNG...")), headers=h).status_code == 422
    assert client.put(url, json=_registro("juan", modelo="otro"), headers=h).status_code == 422


def test_rostro_de_otra_empresa(client, datos):
    r = _registrar(client, datos, trabajador_key="trabajador_otra")
    assert r.status_code == 404


def test_solo_admin_borra_rostro(client, datos, db):
    _registrar(client, datos)
    url = f"/trabajadores/{datos['trabajador'].id}/rostro"
    assert client.delete(url, headers=headers_de(client, "super@test.com")).status_code == 403
    db.refresh(datos["trabajador"])
    ruta = datos["trabajador"].foto_registro_path
    assert ruta and fotos.leer(ruta) is not None
    r = client.delete(url, headers=headers_de(client, "admin@test.com"))
    assert r.status_code == 200
    assert r.json()["rostro_registrado"] is False
    assert fotos.leer(ruta) is None


def test_foto_de_registro_se_puede_ver(client, datos):
    _registrar(client, datos)
    h = headers_de(client, "admin@test.com")
    r = client.get(f"/trabajadores/{datos['trabajador'].id}/rostro/foto", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == _foto("juan")


def test_kiosco_recibe_huellas(client, datos):
    _registrar(client, datos)
    trab = client.get("/kiosco/config", headers=_dev(datos)).json()["trabajadores"]
    assert trab[0]["huella_b64"] == _huella_b64("juan")


# --- Marcaje facial -----------------------------------------------------------

def test_marcaje_facial_que_coincide(client, datos, db):
    _registrar(client, datos)
    res = _lote(client, datos, [_item_facial(datos, "juan")])
    assert res[0]["estado"] == "creado"
    m = db.query(models.Marcaje).one()
    assert m.metodo == "rostro"
    assert m.estado_sync == "synced"
    assert m.similitud == 0.8
    assert m.similitud_servidor == pytest.approx(1.0, abs=1e-5)
    assert m.prueba_vida == "parpadeo"
    assert fotos.leer(m.foto_path) == _foto("juan")


def test_marcaje_facial_con_otra_cara_queda_en_conflicto(client, datos, db):
    _registrar(client, datos)
    res = _lote(client, datos, [_item_facial(datos, "impostor")])
    assert res[0]["estado"] == "creado"  # no se pierde: queda para revisión con la foto
    m = db.query(models.Marcaje).one()
    assert m.estado_sync == "conflicto"
    assert "no coincide" in m.razon_conflicto
    assert m.similitud_servidor < rostros.UMBRAL_SIMILITUD


def test_marcaje_facial_sin_foto(client, datos, db):
    _registrar(client, datos)
    item = _item_facial(datos, "juan")
    item.pop("foto_jpeg_b64")
    _lote(client, datos, [item])
    assert "sin foto" in db.query(models.Marcaje).one().razon_conflicto


def test_marcaje_manual_en_kiosco_de_trabajador_con_rostro(client, datos, db):
    _registrar(client, datos)
    item = payload_marcaje(datos, "entrada", id=str(uuid.uuid4()))
    item.pop("origen")
    _lote(client, datos, [item])
    m = db.query(models.Marcaje).one()
    assert m.metodo == "manual"
    assert m.estado_sync == "conflicto"
    assert "sin reconocimiento facial" in m.razon_conflicto


def test_marcaje_manual_sin_rostro_registrado_es_normal(client, datos, db):
    item = payload_marcaje(datos, "entrada", id=str(uuid.uuid4()))
    item.pop("origen")
    _lote(client, datos, [item])
    assert db.query(models.Marcaje).one().estado_sync == "synced"


def test_supervisor_marca_manual_sin_conflicto(client, datos, db):
    _registrar(client, datos)
    h = headers_de(client, "super@test.com")
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada"), headers=h)
    assert r.status_code == 200
    assert r.json()["estado_sync"] == "synced"


def test_servidor_sin_modelo_acepta_sin_verificar(client, datos, db):
    _registrar(client, datos)
    rostros.fijar_embebedor(None)
    _lote(client, datos, [_item_facial(datos, "impostor")])
    m = db.query(models.Marcaje).one()
    assert m.estado_sync == "synced"
    assert m.similitud_servidor is None


def test_foto_del_marcaje(client, datos, db):
    _registrar(client, datos)
    res = _lote(client, datos, [_item_facial(datos, "juan")])
    h = headers_de(client, "admin@test.com")
    r = client.get(f"/marcajes/{res[0]['id']}/foto", headers=h)
    assert r.status_code == 200 and r.content == _foto("juan")
    h_otra = headers_de(client, "admin@otra.com")
    assert client.get(f"/marcajes/{res[0]['id']}/foto", headers=h_otra).status_code == 404


def test_foto_no_jpeg_rechazada(client, datos):
    item = _item_facial(datos, "juan")
    item["foto_jpeg_b64"] = _b64(b"GIF89a....")
    res = _lote(client, datos, [item])
    assert res[0]["estado"] == "rechazado" and res[0]["codigo"] == 422


def test_metodo_invalido(client, datos):
    res = _lote(client, datos, [_item_facial(datos, "juan", metodo="huella_dactilar")])
    assert res[0]["estado"] == "rechazado"


def test_reenvio_no_duplica_foto(client, datos, db):
    _registrar(client, datos)
    item = _item_facial(datos, "juan")
    _lote(client, datos, [item])
    res = _lote(client, datos, [item])
    assert res[0]["estado"] == "duplicado"
    assert db.query(models.Marcaje).count() == 1


def test_rutas_de_foto_no_salen_del_directorio():
    assert fotos.leer("../../etc/passwd") is None


# --- Modelo real (solo si está descargado) ----------------------------------------

_MODELO = os.path.join(os.path.dirname(rostros.__file__), "modelos", "face_recognition_sface_2021dec.onnx")


@pytest.mark.skipif(not os.path.exists(_MODELO), reason="modelo SFace no descargado")
def test_modelo_sface_real():
    import cv2
    import numpy as np

    motor = rostros.Embebedor(_MODELO)
    # Imagen suave (las fotos reales no son ruido puro) de 224x224, como la manda el teléfono
    ruido = (np.random.RandomState(1).rand(224, 224, 3) * 255).astype(np.uint8)
    img224 = cv2.GaussianBlur(ruido, (0, 0), 6)
    img224 = cv2.normalize(img224, None, 0, 255, cv2.NORM_MINMAX)
    # El teléfono calcula su huella con la reducción 2x2 (promedio) a 112x112
    img112 = cv2.resize(img224, (112, 112), interpolation=cv2.INTER_AREA)
    h_tel = rostros.normalizar(motor._rec.feature(img112))
    assert h_tel.shape == (128,)
    ok, jpeg = cv2.imencode(".jpg", img224, [cv2.IMWRITE_JPEG_QUALITY, 90])
    h_srv = motor.huella(jpeg.tobytes())
    assert np.linalg.norm(h_srv) == pytest.approx(1.0, abs=1e-5)
    # La huella del servidor (desde el JPEG) coincide con la del teléfono
    assert rostros.similitud(h_tel, h_srv) > 0.95
