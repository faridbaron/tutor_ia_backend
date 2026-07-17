import logging
from sqlalchemy.orm import Session

from models import StudentProgress, DiagnosticoSesion, Nivel
from services import neo4j_service
from services.progress_service import NIVEL_MAX_DIFICULTAD

log = logging.getLogger(__name__)


# ── helpers compartidos (usados por estudio_router y ruta_router) ────

def _nivel_estudiante(node_id: str, student_id: int, db: Session) -> str:
    info = neo4j_service.get_nodos_info([node_id])
    unidad_str = info.get(node_id, {}).get("unidad", "UNIDAD 1")
    try:
        unidad_num = int(unidad_str.split()[-1])
    except (ValueError, IndexError):
        unidad_num = 1
    sesion = (
        db.query(DiagnosticoSesion)
        .filter(
            DiagnosticoSesion.student_id == student_id,
            DiagnosticoSesion.unidad_id  == f"unidad_{unidad_num}",
            DiagnosticoSesion.estado     == "completado",
        )
        .order_by(DiagnosticoSesion.fecha_fin.desc())
        .first()
    )
    if sesion and sesion.nivel_resultado_global:
        return sesion.nivel_resultado_global.value
    return Nivel.BASICO.value


def _siguiente_nodo(node_id: str, nivel: str, student_id: int, db: Session):
    info = neo4j_service.get_nodos_info([node_id])
    unidad_str = info.get(node_id, {}).get("unidad", "UNIDAD 1")
    try:
        unidad_num = int(unidad_str.split()[-1])
    except (ValueError, IndexError):
        unidad_num = 1
    dificultad = NIVEL_MAX_DIFICULTAD[nivel]
    nodos      = neo4j_service.get_nodos_unidad_nivel(unidad_num, dificultad)
    if not nodos:
        return None
    ids_set     = {n["tema_canonico"] for n in nodos}
    all_prereqs = neo4j_service.get_prereqs_nodos(list(ids_set))
    local_pre   = {tc: [p for p in all_prereqs.get(tc, []) if p in ids_set] for tc in ids_set}
    dominados   = {
        r.node_id
        for r in db.query(StudentProgress).filter(
            StudentProgress.student_id == student_id,
            StudentProgress.node_id.in_(list(ids_set)),
            StudentProgress.dominado   == True,
        ).all()
    }
    for n in nodos:
        tc = n["tema_canonico"]
        if tc not in dominados and all(p in dominados for p in local_pre.get(tc, [])):
            return tc
    return None
