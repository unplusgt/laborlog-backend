import uuid
from datetime import timedelta

from laborlog_backend import models

from .conftest import OBRA_LAT, OBRA_LNG, ahora_local, headers_de, payload_marcaje

# ~0.001 grados de latitud ≈ 111 m
LAT_FUERA = OBRA_LAT + 0.001
LAT_CERCA = OBRA_LAT + 0.0002  # ≈ 22 m


def _marcaje_previo(db, datos, tipo, horas_atras):
    m = models.Marcaje(
        id=uuid.uuid4(), empresa_id=datos["empresa"].id, trabajador_id=datos["trabajador"].id,
        obra_id=datos["obra"].id, tipo=tipo, origen=datos["dispositivo"].id,
        timestamp_local=ahora_local() - timedelta(hours=horas_atras),
        lat=str(OBRA_LAT), lng=str(OBRA_LNG), estado_sync="synced",
    )
    db.add(m)
    db.commit()


# --- Autenticación del marcaje ------------------------------------------------

def test_marcaje_sin_autenticacion_rechazado(client, datos):
    r = client.post("/marcajes/", json=payload_marcaje(datos))
    assert r.status_code == 401


def test_marcaje_con_token_dispositivo_invalido(client, datos):
    r = client.post("/marcajes/", json=payload_marcaje(datos), headers={"X-Device-Token": "falso"})
    assert r.status_code == 401


def test_supervisor_registra_marcaje(client, datos):
    headers = headers_de(client, "super@test.com")
    r = client.post("/marcajes/", json=payload_marcaje(datos, lat=LAT_CERCA), headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tipo"] == "entrada"
    assert body["estado_sync"] == "synced"
    assert body["registrado_por"] == str(datos["supervisor"].id)
    assert body["origen"] == str(datos["dispositivo"].id)


def test_dispositivo_registra_marcaje_y_se_usa_su_id(client, datos, db):
    payload = payload_marcaje(datos)
    payload["origen"] = str(uuid.uuid4())  # se ignora: manda el token
    r = client.post("/marcajes/", json=payload, headers={"X-Device-Token": datos["device_token"]})
    assert r.status_code == 200, r.text
    assert r.json()["origen"] == str(datos["dispositivo"].id)
    assert r.json()["registrado_por"] is None
    db.refresh(datos["dispositivo"])
    assert datos["dispositivo"].last_sync is not None


def test_dispositivo_desactivado(client, datos, db):
    datos["dispositivo"].activo = False
    db.commit()
    r = client.post("/marcajes/", json=payload_marcaje(datos), headers={"X-Device-Token": datos["device_token"]})
    assert r.status_code == 403


def test_no_puede_marcar_trabajador_de_otra_empresa(client, datos):
    headers = headers_de(client, "super@test.com")
    payload = payload_marcaje(datos, trabajador_id=str(datos["trabajador_otra"].id))
    assert client.post("/marcajes/", json=payload, headers=headers).status_code == 404


def test_trabajador_inactivo(client, datos):
    headers = headers_de(client, "super@test.com")
    payload = payload_marcaje(datos, trabajador_id=str(datos["inactivo"].id))
    assert client.post("/marcajes/", json=payload, headers=headers).status_code == 409


# --- Geocerca -------------------------------------------------------------------

def test_fuera_del_radio_rechazado(client, datos):
    headers = headers_de(client, "super@test.com")
    r = client.post("/marcajes/", json=payload_marcaje(datos, lat=LAT_FUERA), headers=headers)
    assert r.status_code == 422
    assert "Fuera del área" in r.json()["error"]


def test_sin_gps_rechazado(client, datos):
    headers = headers_de(client, "super@test.com")
    payload = payload_marcaje(datos)
    payload["lat"] = None
    payload["lng"] = None
    assert client.post("/marcajes/", json=payload, headers=headers).status_code == 422


def test_obra_sin_ubicacion_rechazada(client, datos):
    headers = headers_de(client, "super@test.com")
    payload = payload_marcaje(datos, obra_id=datos["obra_sin_gps"].id)
    r = client.post("/marcajes/", json=payload, headers=headers)
    assert r.status_code == 422
    assert "ubicación GPS" in r.json()["error"]


def test_coordenadas_basura(client, datos):
    headers = headers_de(client, "super@test.com")
    payload = payload_marcaje(datos)
    payload["lat"] = "abc"
    assert client.post("/marcajes/", json=payload, headers=headers).status_code == 422


# --- Secuencia entrada/salida -------------------------------------------------------

def test_no_puede_empezar_con_salida(client, datos):
    headers = headers_de(client, "super@test.com")
    r = client.post("/marcajes/", json=payload_marcaje(datos, tipo="salida"), headers=headers)
    assert r.status_code == 409


def test_entrada_duplicada_rechazada(client, datos):
    headers = headers_de(client, "super@test.com")
    assert client.post("/marcajes/", json=payload_marcaje(datos), headers=headers).status_code == 200
    r = client.post("/marcajes/", json=payload_marcaje(datos), headers=headers)
    assert r.status_code == 409
    assert "debe marcar SALIDA" in r.json()["error"]


def test_entrada_y_salida(client, datos):
    headers = headers_de(client, "super@test.com")
    assert client.post("/marcajes/", json=payload_marcaje(datos), headers=headers).status_code == 200
    r = client.post("/marcajes/", json=payload_marcaje(datos, tipo="SALIDA"), headers=headers)
    assert r.status_code == 200
    assert r.json()["tipo"] == "salida"


def test_turno_nocturno_salida_al_dia_siguiente(client, datos, db):
    # Entrada hace 9 horas (p. ej. 22:00 de ayer) y salida ahora: debe permitirse
    _marcaje_previo(db, datos, "entrada", horas_atras=9)
    headers = headers_de(client, "super@test.com")
    r = client.post("/marcajes/", json=payload_marcaje(datos, tipo="salida"), headers=headers)
    assert r.status_code == 200, r.text


def test_entrada_olvidada_hace_mas_de_un_turno(client, datos, db):
    # Una ENTRADA sin SALIDA de hace 30 h no bloquea la entrada de hoy
    _marcaje_previo(db, datos, "entrada", horas_atras=30)
    headers = headers_de(client, "super@test.com")
    r = client.post("/marcajes/", json=payload_marcaje(datos, tipo="entrada"), headers=headers)
    assert r.status_code == 200, r.text


# --- Hora del dispositivo -------------------------------------------------------------

def test_hora_adelantada_rechazada(client, datos):
    headers = headers_de(client, "super@test.com")
    ts = ahora_local() + timedelta(minutes=30)
    r = client.post("/marcajes/", json=payload_marcaje(datos, ts=ts), headers=headers)
    assert r.status_code == 422
    assert "adelantada" in r.json()["error"]


def test_hora_atrasada_se_acepta_como_conflicto(client, datos):
    headers = headers_de(client, "super@test.com")
    ts = ahora_local() - timedelta(hours=2)
    r = client.post("/marcajes/", json=payload_marcaje(datos, ts=ts), headers=headers)
    assert r.status_code == 200
    assert r.json()["estado_sync"] == "conflicto"
    assert "atrasada" in r.json()["razon_conflicto"]


def test_hora_con_zona_horaria_se_normaliza(client, datos):
    from datetime import datetime, timezone
    headers = headers_de(client, "super@test.com")
    ts_utc = datetime.now(timezone.utc)
    payload = payload_marcaje(datos)
    payload["timestamp_local"] = ts_utc.isoformat()
    r = client.post("/marcajes/", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["estado_sync"] == "synced"


def test_cliente_no_puede_fijar_estado(client, datos):
    headers = headers_de(client, "super@test.com")
    payload = payload_marcaje(datos, estado_sync="synced", razon_conflicto="ninguna")
    payload["timestamp_local"] = (ahora_local() - timedelta(hours=3)).isoformat()
    r = client.post("/marcajes/", json=payload, headers=headers)
    assert r.json()["estado_sync"] == "conflicto"


# --- Tokens de dispositivo ------------------------------------------------------------

def test_crear_dispositivo_devuelve_token_una_vez(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.post("/dispositivos/", json={"nombre": "Tablet"}, headers=headers)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert len(token) >= 40
    dispositivo_id = r.json()["id"]

    lista = client.get("/dispositivos/", headers=headers).json()
    assert all("token" not in d and "token_hash" not in d for d in lista)

    # El token sirve para marcar
    r = client.post("/marcajes/", json=payload_marcaje(datos), headers={"X-Device-Token": token})
    assert r.status_code == 200
    assert r.json()["origen"] == dispositivo_id


def test_rotar_token_invalida_el_anterior(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.post(f"/dispositivos/{datos['dispositivo'].id}/token", headers=headers)
    assert r.status_code == 200
    nuevo = r.json()["token"]
    viejo = datos["device_token"]
    assert client.post("/marcajes/", json=payload_marcaje(datos), headers={"X-Device-Token": viejo}).status_code == 401
    assert client.post("/marcajes/", json=payload_marcaje(datos), headers={"X-Device-Token": nuevo}).status_code == 200


def test_supervisor_no_crea_dispositivos(client, datos):
    headers = headers_de(client, "super@test.com")
    assert client.post("/dispositivos/", json={"nombre": "X"}, headers=headers).status_code == 403
