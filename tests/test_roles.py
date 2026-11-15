"""Cuentas por empresa y roles: plataforma, superadmin, jefe y supervisor."""
import uuid

from laborlog_backend import models

from .conftest import PASSWORD, ahora_local, headers_de, payload_marcaje

PERIODO = {"fecha_inicio": "2026-01-01T00:00:00", "fecha_fin": "2026-12-31T00:00:00"}


def _login(client, empresa, usuario, password=PASSWORD):
    return client.post("/login", data={"username": usuario, "password": password, "empresa": empresa})


# --- Login con empresa -----------------------------------------------------------

def test_login_con_usuario_o_email(client, datos):
    assert _login(client, "test", "admin").status_code == 200
    assert _login(client, "test", "admin@test.com").status_code == 200
    assert _login(client, "TEST", "ADMIN").status_code == 200  # sin distinguir mayúsculas


def test_login_exige_la_empresa_correcta(client, datos):
    assert _login(client, "otra", "admin@test.com").status_code == 401
    assert _login(client, "", "admin@test.com").status_code == 401
    assert _login(client, "no-existe", "admin").status_code == 401
    r = _login(client, "test", "admin", "mala")
    assert r.status_code == 401
    assert "incorrectos" in r.json()["error"]


def test_mismo_usuario_en_dos_empresas(client, datos):
    # "admin" existe en las dos empresas de prueba: la empresa decide a cuál se entra
    h_test = headers_de(client, "admin", empresa="test")
    h_otra = headers_de(client, "admin", empresa="otra")
    assert client.get("/me", headers=h_test).json()["empresa_codigo"] == "test"
    assert client.get("/me", headers=h_otra).json()["empresa_codigo"] == "otra"


def test_usuario_desactivado_no_entra(client, datos, db):
    h = headers_de(client, "super@test.com")
    datos["supervisor"].activo = False
    db.commit()
    assert _login(client, "test", "super").status_code == 401
    assert client.get("/obras", headers=h).status_code == 401  # su token anterior deja de servir


def test_empresa_desactivada(client, datos, db):
    h = headers_de(client, "admin@test.com")
    datos["empresa"].activa = False
    db.commit()
    assert _login(client, "test", "admin").status_code == 401
    assert client.get("/obras", headers=h).status_code == 403
    r = client.get("/kiosco/config", headers={"X-Device-Token": datos["device_token"]})
    assert r.status_code == 403


def test_login_estilo_docs(client, datos):
    r = client.post("/login", data={"username": "test/admin", "password": PASSWORD})
    assert r.status_code == 200


# --- Permisos por rol ------------------------------------------------------------

def test_jefe_todo_menos_planilla(client, datos):
    h = headers_de(client, "admin@test.com")
    assert client.post("/trabajadores", json={"nombre": "Nuevo", "obra_id": 1}, headers=h).status_code == 200
    assert client.post("/obras", json={"nombre": "Nueva"}, headers=h).status_code == 200
    assert client.get("/dispositivos", headers=h).status_code == 200
    assert client.get("/usuarios_admin", headers=h).status_code == 200
    assert client.get("/planillas/resumen", params=PERIODO, headers=h).status_code == 403
    assert client.put("/empresas/me/config", json={"hora_extra_desde": 9}, headers=h).status_code == 403


def test_superadmin_ve_planilla_y_configura(client, datos):
    h = headers_de(client, "root@test.com")
    assert client.get("/planillas/resumen", params=PERIODO, headers=h).status_code == 200
    assert client.put("/empresas/me/config", json={"hora_extra_desde": 9}, headers=h).status_code == 200


def test_jefe_solo_gestiona_supervisores(client, datos):
    h = headers_de(client, "admin@test.com")
    nuevo = {"nombre": "J2", "usuario": "jefe2", "password": PASSWORD, "rol": "jefe"}
    assert client.post("/usuarios_admin", json=nuevo, headers=h).status_code == 403
    sup = {"nombre": "S2", "usuario": "super2", "password": PASSWORD, "rol": "supervisor", "obras": [1]}
    r = client.post("/usuarios_admin", json=sup, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["obras"] == [1]
    # No puede tocar al superadmin
    r = client.put(f"/usuarios_admin/{datos['superadmin'].id}", json={"nombre": "X"}, headers=h)
    assert r.status_code == 403
    assert client.delete(f"/usuarios_admin/{datos['superadmin'].id}", headers=h).status_code == 403


def test_superadmin_crea_jefe_sin_email(client, datos):
    h = headers_de(client, "root@test.com")
    r = client.post("/usuarios_admin",
                    json={"nombre": "Jefe Obra", "usuario": "jefe.obra", "password": PASSWORD, "rol": "jefe"},
                    headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["email"] is None
    assert _login(client, "test", "jefe.obra").status_code == 200


def test_usuario_duplicado_y_formato(client, datos):
    h = headers_de(client, "root@test.com")
    dup = {"nombre": "X", "usuario": "admin", "password": PASSWORD, "rol": "supervisor"}
    assert client.post("/usuarios_admin", json=dup, headers=h).status_code == 409
    malo = {"nombre": "X", "usuario": "Con Espacios", "password": PASSWORD, "rol": "supervisor"}
    assert client.post("/usuarios_admin", json=malo, headers=h).status_code == 422


def test_no_puede_cambiarse_el_rol_ni_desactivarse(client, datos):
    h = headers_de(client, "root@test.com")
    url = f"/usuarios_admin/{datos['superadmin'].id}"
    assert client.put(url, json={"activo": False}, headers=h).status_code == 400
    assert client.put(url, json={"rol": "jefe"}, headers=h).status_code == 400


def test_desactivar_supervisor(client, datos):
    h = headers_de(client, "admin@test.com")
    r = client.put(f"/usuarios_admin/{datos['supervisor'].id}", json={"activo": False}, headers=h)
    assert r.status_code == 200
    assert _login(client, "test", "super").status_code == 401


# --- Alcance del supervisor ---------------------------------------------------------

def test_supervisor_solo_ve_sus_obras_y_trabajadores(client, datos, db):
    db.add(models.Obra(id=7, nombre="Ajena", empresa_id=datos["empresa"].id))
    db.flush()
    ajeno = models.Trabajador(id=uuid.uuid4(), nombre="De la obra 7", activo=True,
                              empresa_id=datos["empresa"].id, obra_id=7)
    sin_obra = models.Trabajador(id=uuid.uuid4(), nombre="Sin obra", activo=True, empresa_id=datos["empresa"].id)
    db.add_all([ajeno, sin_obra])
    db.commit()
    h = headers_de(client, "super@test.com")
    assert {o["id"] for o in client.get("/obras", headers=h).json()} == {1, 2}
    nombres = {t["nombre"] for t in client.get("/trabajadores", headers=h).json()}
    assert "Juan Perez" in nombres and "De la obra 7" not in nombres and "Sin obra" not in nombres
    assert client.get(f"/trabajadores/{ajeno.id}", headers=h).status_code == 404
    assert client.get("/obras/7", headers=h).status_code == 404
    assert client.get(f"/trabajadores/{ajeno.id}/marcajes", headers=h).status_code == 404
    # El jefe ve todo
    hj = headers_de(client, "admin@test.com")
    assert len(client.get("/trabajadores", headers=hj).json()) == 4


def test_supervisor_no_ve_dispositivos(client, datos):
    h = headers_de(client, "super@test.com")
    assert client.get("/dispositivos", headers=h).status_code == 403


def test_supervisor_no_marca_fuera_de_sus_obras(client, datos, db):
    db.add(models.Obra(id=7, nombre="Ajena", lat="14.538364", lng="-90.478339", radio_metros=50,
                       empresa_id=datos["empresa"].id))
    db.commit()
    h = headers_de(client, "super@test.com")
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada", obra_id=7), headers=h)
    assert r.status_code == 403


def test_supervisor_solo_ve_marcajes_de_sus_obras(client, datos, db):
    db.add(models.Obra(id=7, nombre="Ajena", empresa_id=datos["empresa"].id))
    db.add(models.Marcaje(id=uuid.uuid4(), empresa_id=datos["empresa"].id, trabajador_id=datos["trabajador"].id,
                          obra_id=7, tipo="entrada", timestamp_local=ahora_local()))
    db.commit()
    h = headers_de(client, "super@test.com")
    assert client.get("/marcajes", headers=h).json() == []
    assert len(client.get("/marcajes", headers=headers_de(client, "admin@test.com")).json()) == 1


def test_me_del_supervisor_incluye_sus_obras(client, datos):
    body = client.get("/me", headers=headers_de(client, "super@test.com")).json()
    assert body["rol"] == "supervisor"
    assert sorted(body["obras"]) == [1, 2]
    assert body["usuario"] == "super"


def test_reasignar_obras_al_supervisor(client, datos):
    h = headers_de(client, "admin@test.com")
    r = client.put(f"/usuarios_admin/{datos['supervisor'].id}", json={"obras": [2]}, headers=h)
    assert r.json()["obras"] == [2]
    hs = headers_de(client, "super@test.com")
    assert [o["id"] for o in client.get("/obras", headers=hs).json()] == [2]


def test_trabajador_en_otra_obra_queda_en_conflicto(client, datos, db):
    db.add(models.Obra(id=3, nombre="Obra 3", lat="14.538364", lng="-90.478339", radio_metros=50,
                       empresa_id=datos["empresa"].id))
    db.commit()
    h = headers_de(client, "admin@test.com")
    r = client.post("/marcajes", json=payload_marcaje(datos, "entrada", obra_id=3), headers=h)
    assert r.status_code == 200
    assert r.json()["estado_sync"] == "conflicto"
    assert "otra obra" in r.json()["razon_conflicto"]


def test_kiosco_solo_trabajadores_de_su_obra(client, datos, db):
    db.add(models.Obra(id=7, nombre="Ajena", empresa_id=datos["empresa"].id))
    db.flush()
    db.add_all([
        models.Trabajador(id=uuid.uuid4(), nombre="De la obra 7", activo=True, empresa_id=datos["empresa"].id, obra_id=7),
        models.Trabajador(id=uuid.uuid4(), nombre="Sin obra", activo=True, empresa_id=datos["empresa"].id),
    ])
    datos["dispositivo"].obra_id = 1
    db.commit()
    body = client.get("/kiosco/config", headers={"X-Device-Token": datos["device_token"]}).json()
    assert {t["nombre"] for t in body["trabajadores"]} == {"Juan Perez", "Sin obra"}
    assert body["empresa_codigo"] == "test"


def test_asignar_obra_de_otra_empresa(client, datos, db):
    db.add(models.Obra(id=9, nombre="Ajena", empresa_id=datos["otra"].id))
    db.commit()
    h = headers_de(client, "admin@test.com")
    r = client.put(f"/trabajadores/{datos['trabajador'].id}", json={"nombre": "Juan Perez", "obra_id": 9}, headers=h)
    assert r.status_code == 404
    r = client.put(f"/usuarios_admin/{datos['supervisor'].id}", json={"obras": [9]}, headers=h)
    assert r.status_code == 404


# --- Plataforma ------------------------------------------------------------------------

def _h_plataforma(client):
    return headers_de(client, "duenio", empresa="plataforma")


def test_plataforma_crea_empresa_con_su_superadmin(client, datos):
    h = _h_plataforma(client)
    r = client.post("/plataforma/empresas", json={
        "nombre": "Constructora ABC", "codigo": "constructora-abc",
        "superadmin": {"nombre": "Dueña", "usuario": "duena", "password": PASSWORD},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["codigo"] == "constructora-abc"
    assert r.json()["usuarios"] == 1
    hs = headers_de(client, "duena", empresa="constructora-abc")
    me = client.get("/me", headers=hs).json()
    assert me["rol"] == "superadmin" and me["empresa_nombre"] == "Constructora ABC"
    # La empresa nueva no ve datos de las demás
    assert client.get("/trabajadores", headers=hs).json() == []


def test_plataforma_no_ve_datos_de_empresas(client, datos):
    h = _h_plataforma(client)
    assert client.get("/trabajadores", headers=h).status_code == 403
    assert client.get("/marcajes", headers=h).status_code == 403
    assert client.get("/planillas/resumen", params=PERIODO, headers=h).status_code == 403
    empresas = client.get("/plataforma/empresas", headers=h).json()
    test = next(e for e in empresas if e["codigo"] == "test")
    assert test["trabajadores"] == 2  # solo conteos


def test_solo_plataforma_administra_empresas(client, datos):
    h = headers_de(client, "root@test.com")
    assert client.get("/plataforma/empresas", headers=h).status_code == 403


def test_codigo_de_empresa_unico_y_reservado(client, datos):
    h = _h_plataforma(client)
    base = {"nombre": "X", "superadmin": {"nombre": "A", "usuario": "aaa", "password": PASSWORD}}
    assert client.post("/plataforma/empresas", json={**base, "codigo": "test"}, headers=h).status_code == 409
    assert client.post("/plataforma/empresas", json={**base, "codigo": "plataforma"}, headers=h).status_code == 422
    assert client.post("/plataforma/empresas", json={**base, "codigo": "Con Espacio"}, headers=h).status_code == 422


def test_plataforma_desactiva_empresa(client, datos):
    h = _h_plataforma(client)
    r = client.put(f"/plataforma/empresas/{datos['otra'].id}", json={"activa": False}, headers=h)
    assert r.status_code == 200 and r.json()["activa"] is False
    assert _login(client, "otra", "admin").status_code == 401


def test_supervisor_marca_sin_dispositivo(client, datos):
    h = headers_de(client, "super@test.com")
    data = payload_marcaje(datos, "entrada")
    data.pop("origen")
    r = client.post("/marcajes", json=data, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["origen"] is None
    assert r.json()["registrado_por"] == str(datos["supervisor"].id)
