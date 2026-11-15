"""Qué obras, trabajadores y marcajes puede ver cada usuario.

- superadmin y jefe: toda su empresa.
- supervisor: solo las obras que tiene asignadas y los trabajadores asignados a esas
  obras (y los marcajes de esas obras).
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import auth, models


def es_supervisor(usuario: models.UsuarioAdmin) -> bool:
    return auth.rol_de(usuario) == auth.ROL_SUPERVISOR


def obras_asignadas(db: Session, usuario: models.UsuarioAdmin) -> list[int]:
    return [
        fila.obra_id
        for fila in db.query(models.SupervisorObra).filter(models.SupervisorObra.usuario_id == usuario.id)
    ]


def obras_visibles(db: Session, usuario: models.UsuarioAdmin) -> list[int] | None:
    """None = todas las de la empresa."""
    return obras_asignadas(db, usuario) if es_supervisor(usuario) else None


def filtrar_obras(q, db: Session, usuario: models.UsuarioAdmin):
    ids = obras_visibles(db, usuario)
    return q if ids is None else q.filter(models.Obra.id.in_(ids))


def filtrar_trabajadores(q, db: Session, usuario: models.UsuarioAdmin):
    ids = obras_visibles(db, usuario)
    return q if ids is None else q.filter(models.Trabajador.obra_id.in_(ids))


def filtrar_marcajes(q, db: Session, usuario: models.UsuarioAdmin):
    ids = obras_visibles(db, usuario)
    return q if ids is None else q.filter(models.Marcaje.obra_id.in_(ids))


def puede_ver_obra(db: Session, usuario: models.UsuarioAdmin, obra_id) -> bool:
    ids = obras_visibles(db, usuario)
    return ids is None or obra_id in ids


def puede_ver_trabajador(db: Session, usuario: models.UsuarioAdmin, trabajador: models.Trabajador) -> bool:
    ids = obras_visibles(db, usuario)
    return ids is None or trabajador.obra_id in ids


def exigir_obra(db: Session, usuario: models.UsuarioAdmin, obra_id) -> None:
    if not puede_ver_obra(db, usuario, obra_id):
        raise HTTPException(status_code=404, detail="Obra no encontrada")


def exigir_trabajador(db: Session, usuario: models.UsuarioAdmin, trabajador: models.Trabajador) -> None:
    if not puede_ver_trabajador(db, usuario, trabajador):
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
