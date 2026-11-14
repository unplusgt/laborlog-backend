from fastapi import status

from .conftest import PASSWORD, headers_de


def test_login_y_me(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.get("/me", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "admin@test.com"
    assert body["rol"] == "admin"
    assert body["nombre"] == "admin"


def test_me_sin_token(client, datos):
    assert client.get("/me").status_code == status.HTTP_401_UNAUTHORIZED


def test_login_incorrecto(client, datos):
    r = client.post("/login", data={"username": "admin@test.com", "password": "mala"})
    assert r.status_code == status.HTTP_401_UNAUTHORIZED


def test_token_manipulado(client, datos):
    r = client.get("/me", headers={"Authorization": "Bearer abc.def.ghi"})
    assert r.status_code == 401


def test_respuesta_de_usuarios_no_incluye_hash(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.get("/usuarios_admin/", headers=headers)
    assert r.status_code == 200
    for u in r.json():
        assert "hash_password" not in u
        assert "password" not in u


def test_admin_crea_supervisor_con_password_en_claro(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.post(
        "/usuarios_admin/",
        json={"nombre": "Nuevo", "email": "Nuevo@Test.com", "password": PASSWORD, "rol": "supervisor"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert "hash_password" not in r.json()
    assert r.json()["email"] == "nuevo@test.com"
    # Puede iniciar sesión con la contraseña dada
    headers_de(client, "nuevo@test.com")


def test_admin_no_puede_crear_superadmin(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.post(
        "/usuarios_admin/",
        json={"nombre": "X", "email": "x@test.com", "password": PASSWORD, "rol": "superadmin"},
        headers=headers,
    )
    assert r.status_code == 403


def test_superadmin_puede_crear_superadmin(client, datos):
    headers = headers_de(client, "root@test.com")
    r = client.post(
        "/usuarios_admin/",
        json={"nombre": "X", "email": "x@test.com", "password": PASSWORD, "rol": "superadmin"},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_rol_invalido(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.post(
        "/usuarios_admin/",
        json={"nombre": "X", "email": "x@test.com", "password": PASSWORD, "rol": "jefe"},
        headers=headers,
    )
    assert r.status_code == 422


def test_email_duplicado(client, datos):
    headers = headers_de(client, "admin@test.com")
    r = client.post(
        "/usuarios_admin/",
        json={"nombre": "X", "email": "super@test.com", "password": PASSWORD, "rol": "supervisor"},
        headers=headers,
    )
    assert r.status_code == 409


def test_supervisor_no_gestiona_usuarios_ni_planillas(client, datos):
    headers = headers_de(client, "super@test.com")
    assert client.get("/usuarios_admin/", headers=headers).status_code == 403
    r = client.get(
        "/planillas/resumen",
        params={"fecha_inicio": "2026-01-01T00:00:00", "fecha_fin": "2026-12-31T00:00:00"},
        headers=headers,
    )
    assert r.status_code == 403


def test_supervisor_lee_pero_no_modifica_catalogos(client, datos):
    headers = headers_de(client, "super@test.com")
    assert client.get("/trabajadores/", headers=headers).status_code == 200
    assert client.get("/obras/", headers=headers).status_code == 200
    r = client.post("/trabajadores/", json={"nombre": "Nuevo"}, headers=headers)
    assert r.status_code == 403
    r = client.post("/obras/", json={"nombre": "Nueva"}, headers=headers)
    assert r.status_code == 403


def test_superadmin_puede_ver_planillas(client, datos):
    headers = headers_de(client, "root@test.com")
    r = client.get(
        "/planillas/resumen",
        params={"fecha_inicio": "2026-01-01T00:00:00", "fecha_fin": "2026-12-31T00:00:00"},
        headers=headers,
    )
    assert r.status_code == 200


def test_crear_con_y_sin_barra_final(client, datos):
    headers = headers_de(client, "admin@test.com")
    assert client.post("/trabajadores", json={"nombre": "A"}, headers=headers).status_code == 200
    assert client.post("/trabajadores/", json={"nombre": "B"}, headers=headers).status_code == 200


def test_id_invalido_da_422_no_500(client, datos):
    headers = headers_de(client, "admin@test.com")
    assert client.get("/trabajadores/no-es-uuid", headers=headers).status_code == 422


def test_aislamiento_entre_empresas(client, datos):
    headers = headers_de(client, "admin@otra.com")
    nombres = [t["nombre"] for t in client.get("/trabajadores/", headers=headers).json()]
    assert nombres == ["De otra empresa"]
    r = client.get(f"/trabajadores/{datos['trabajador'].id}", headers=headers)
    assert r.status_code == 404
