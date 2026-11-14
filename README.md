# LaborLog – Backend (FastAPI)

API multi-empresa para control de asistencias en obras de construcción.
Documentación general del proyecto: `../DOCUMENTACION_COMPLETA_LABORLOG.md`.

## Módulos
| Archivo | Contenido |
|---|---|
| `main.py` | Endpoints REST |
| `marcajes.py` | Reglas anti-fraude al registrar marcajes: geocerca, secuencia entrada/salida (turnos de hasta 20 h), reloj del dispositivo |
| `auth.py` | JWT, roles (`superadmin`, `admin`, `supervisor`), tokens de dispositivo (`X-Device-Token`) |
| `models.py` / `schemas.py` | SQLAlchemy / Pydantic |
| `database.py` | Conexión (`DATABASE_URL`) |
| `migrations/` | Alembic |
| `create_admin_user.py` | Crear o restablecer un usuario |

## Desarrollo local
Desde la **raíz del proyecto** (donde están `.env`, `requirements.txt` y `alembic.ini`):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\alembic upgrade head
.venv\Scripts\uvicorn laborlog_backend.main:app --reload
```

## Pruebas
```powershell
.venv\Scripts\python -m pytest
```
Usan una BD SQLite temporal; no tocan Postgres.

## Cambios de esquema
1. Editar `models.py`
2. `alembic revision -m "descripcion"` y escribir `upgrade()` / `downgrade()`
3. `alembic upgrade head`

## Autenticación de marcajes
`POST /marcajes` acepta:
- `Authorization: Bearer <jwt>` de un usuario con rol supervisor/admin/superadmin (app web), o
- `X-Device-Token: <token>` de un dispositivo registrado (app móvil). El token se obtiene al crear el dispositivo (`POST /dispositivos`) o al rotarlo (`POST /dispositivos/{id}/token`) y solo se muestra una vez.
