"""Fase 1: cola offline (POST /marcajes/lote), modo kiosco y GPS falso."""
import uuid
from datetime import timedelta

from laborlog_backend import models

from .conftest import OBRA_LAT, OBRA_LNG, ahora_local, headers_de, payload_marcaje


def _dev(datos):
    return {"X-Device-Token": datos["device_token"]}


def _item(datos, tipo="entrada", ts=None, **extra):
    data = payload_marcaje(datos, tipo=tipo, ts=ts, **extra)
    data.setdefault("id", str(uuid.uuid4()))
    data.pop("origen")
    return data


def _lote(client, datos, items, enviado_en=None):
    r = client.post(
        "/marcajes/lote",
        json={"enviado_en": (enviado_en or ahora_local()).isoformat(), "marcajes": items},
        headers=_dev(datos),
    )
    assert r.status_code == 200, r.text
    return r.json()["resultados"]


# --- Lote --------------------------------------------------------------------

def test_lote_crea_marcajes_offline(client, datos, db):
    ahora = ahora_local()
    items = [
        _item(datos, "entrada", ts=ahora - timedelta(hours=3)),
        _item(datos, "salida", ts=ahora - timedelta(minutes=30)),
    ]
    res = _lote(client, datos, items)
    assert [r["estado"] for r in res] == ["creado", "creado"]
    assert {r["id"] for r in res} == {i["id"] for i in items}
    for r in res:
        assert r["marcaje"]["capturado_offline"] is True
        assert r["marcaje"]["estado_sync"] == "synced"  # el retraso offline no es conflicto


def test_reenviar_lote_no_duplica(client, datos, db):
    items = [_item(datos, "entrada", ts=ahora_local() - timedelta(minutes=10))]
    _lote(client, datos, items)
    res = _lote(client, datos, items)
    assert res[0]["estado"] == "duplicado"
    assert db.query(models.Marcaje).count() == 1


def test_post_individual_con_mismo_id_es_idempotente(client, datos, db):
    h = headers_de(client, "super@test.com")
    data = payload_marcaje(datos, "entrada", id=str(uuid.uuid4()))
    r1 = client.post("/marcajes", json=data, headers=h)
    r2 = client.post("/marcajes", json=data, headers=h)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"] == data["id"]
    assert db.query(models.Marcaje).count() == 1


def test_lote_se_procesa_en_orden_cronologico(client, datos):
    ahora = ahora_local()
    # La salida viene primero en la lista, pero es posterior a la entrada
    items = [
        _item(datos, "salida", ts=ahora - timedelta(hours=1)),
        _item(datos, "entrada", ts=ahora - timedelta(hours=9)),
    ]
    res = _lote(client, datos, items)
    assert all(r["estado"] == "creado" for r in res)


def test_lote_rechaza_uno_y_acepta_los_demas(client, datos, db):
    ahora = ahora_local()
    lejos = _item(datos, "entrada", ts=ahora - timedelta(hours=2), lat=OBRA_LAT + 0.01)
    bien = _item(datos, "entrada", ts=ahora - timedelta(hours=1))
    res = {r["id"]: r for r in _lote(client, datos, [lejos, bien])}
    assert res[lejos["id"]]["estado"] == "rechazado"
    assert res[lejos["id"]]["codigo"] == 422
    assert "Fuera del área" in res[lejos["id"]]["error"]
    assert res[bien["id"]]["estado"] == "creado"
    assert db.query(models.Marcaje).count() == 1


def test_reloj_atrasado_se_corrige_y_queda_en_conflicto(client, datos, db):
    ahora = ahora_local()
    desfase = timedelta(hours=2)  # el teléfono va 2 h atrasado
    capturado_real = ahora - timedelta(minutes=30)
    items = [_item(datos, "entrada", ts=capturado_real - desfase)]
    res = _lote(client, datos, items, enviado_en=ahora - desfase)
    assert res[0]["estado"] == "creado"
    m = db.query(models.Marcaje).one()
    assert abs(m.timestamp_local - capturado_real) < timedelta(minutes=1)
    assert m.estado_sync == "conflicto"
    assert "atrasado" in m.razon_conflicto


def test_reloj_adelantado_se_corrige(client, datos, db):
    ahora = ahora_local()
    desfase = timedelta(hours=1)  # el teléfono va 1 h adelantado
    capturado_real = ahora - timedelta(minutes=20)
    res = _lote(client, datos, [_item(datos, "entrada", ts=capturado_real + desfase)],
                enviado_en=ahora + desfase)
    assert res[0]["estado"] == "creado"
    m = db.query(models.Marcaje).one()
    assert abs(m.timestamp_local - capturado_real) < timedelta(minutes=1)
    assert "adelantado" in m.razon_conflicto


def test_marcaje_con_hora_futura_rechazado(client, datos):
    ahora = ahora_local()
    res = _lote(client, datos, [_item(datos, "entrada", ts=ahora + timedelta(hours=1))], enviado_en=ahora)
    assert res[0]["estado"] == "rechazado"
    assert "futura" in res[0]["error"]


def test_sincronizado_muy_tarde_queda_en_conflicto(client, datos, db):
    res = _lote(client, datos, [_item(datos, "entrada", ts=ahora_local() - timedelta(days=9))])
    assert res[0]["estado"] == "creado"
    assert "días después" in db.query(models.Marcaje).one().razon_conflicto


def test_marcaje_tardio_no_deja_dos_entradas_seguidas(client, datos):
    h = headers_de(client, "super@test.com")
    ahora = ahora_local()
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada", ts=ahora - timedelta(minutes=5)), headers=h)
    assert r.status_code == 200, r.text
    # Llega después una ENTRADA offline capturada antes que la del supervisor
    res = _lote(client, datos, [_item(datos, "entrada", ts=ahora - timedelta(hours=1))])
    assert res[0]["estado"] == "rechazado"
    assert res[0]["codigo"] == 409
    assert "posterior" in res[0]["error"]


def test_id_de_otra_empresa_rechazado(client, datos, db):
    ajeno = models.Marcaje(
        id=uuid.uuid4(), empresa_id=datos["otra"].id, trabajador_id=datos["trabajador_otra"].id,
        obra_id=None, tipo="entrada", timestamp_local=ahora_local(),
    )
    db.add(ajeno)
    db.commit()
    res = _lote(client, datos, [_item(datos, "entrada", id=str(ajeno.id))])
    assert res[0]["estado"] == "rechazado"
    assert res[0]["codigo"] == 409


def test_lote_sin_autenticacion(client, datos):
    r = client.post("/marcajes/lote", json={"enviado_en": ahora_local().isoformat(), "marcajes": []})
    assert r.status_code == 401


def test_lote_actualiza_last_sync(client, datos, db):
    _lote(client, datos, [])
    db.refresh(datos["dispositivo"])
    assert datos["dispositivo"].last_sync is not None


# --- GPS falso -----------------------------------------------------------------

def test_ubicacion_simulada_rechazada(client, datos):
    h = headers_de(client, "super@test.com")
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada", ubicacion_simulada=True), headers=h)
    assert r.status_code == 422
    assert "simulada" in r.json()["error"]


def test_precision_gps_se_guarda(client, datos):
    h = headers_de(client, "super@test.com")
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada", precision_metros=12.5), headers=h)
    assert r.status_code == 200
    assert r.json()["precision_metros"] == 12.5


# --- Kiosco ---------------------------------------------------------------------

def test_dispositivo_asignado_solo_marca_en_su_obra(client, datos, db):
    otra_obra = models.Obra(id=3, nombre="Otra obra", lat=str(OBRA_LAT), lng=str(OBRA_LNG),
                            radio_metros=50, empresa_id=datos["empresa"].id)
    db.add(otra_obra)
    datos["dispositivo"].obra_id = otra_obra.id
    db.commit()
    res = _lote(client, datos, [_item(datos, "entrada")])  # obra 1
    assert res[0]["codigo"] == 403
    res = _lote(client, datos, [_item(datos, "entrada", obra_id=3)])
    assert res[0]["estado"] == "creado"


def test_kiosco_config(client, datos, db):
    datos["dispositivo"].obra_id = datos["obra"].id
    db.commit()
    r = client.get("/kiosco/config", headers=_dev(datos))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["obra"]["nombre"] == "BARI"
    assert body["obra"]["radio_metros"] == 50
    assert body["zona_horaria"] == "America/Guatemala"
    nombres = [t["nombre"] for t in body["trabajadores"]]
    assert nombres == ["Juan Perez"]  # ni inactivos ni de otra empresa
    assert "embedding" not in body["trabajadores"][0]


def test_kiosco_config_incluye_ultimo_marcaje(client, datos):
    h = headers_de(client, "super@test.com")
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada"), headers=h)
    assert r.status_code == 200
    t = client.get("/kiosco/config", headers=_dev(datos)).json()["trabajadores"][0]
    assert t["ultimo_tipo"] == "entrada"
    assert t["ultimo_marcaje"].endswith("Z") or "+00:00" in t["ultimo_marcaje"]


def test_kiosco_config_sin_obra_asignada(client, datos):
    r = client.get("/kiosco/config", headers=_dev(datos))
    assert r.status_code == 200
    assert r.json()["obra"] is None


def test_kiosco_config_exige_token_de_dispositivo(client, datos):
    assert client.get("/kiosco/config").status_code == 401
    assert client.get("/kiosco/config", headers={"X-Device-Token": "malo"}).status_code == 401
    h = headers_de(client, "admin@test.com")
    assert client.get("/kiosco/config", headers=h).status_code == 401


def test_admin_asigna_obra_al_dispositivo(client, datos, db):
    h = headers_de(client, "admin@test.com")
    url = f"/dispositivos/{datos['dispositivo'].id}"
    r = client.put(url, json={"obra_id": datos["obra"].id}, headers=h)
    assert r.status_code == 200
    assert r.json()["obra_id"] == datos["obra"].id
    # Obra de otra empresa: no existe para este admin
    ajena = models.Obra(id=9, nombre="Ajena", empresa_id=datos["otra"].id)
    db.add(ajena)
    db.commit()
    assert client.put(url, json={"obra_id": 9}, headers=h).status_code == 404
    r = client.post("/dispositivos", json={"nombre": "Nuevo", "obra_id": 9}, headers=h)
    assert r.status_code == 404
