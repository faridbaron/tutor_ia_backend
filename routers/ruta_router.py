import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from models import StudentProgress, DiagnosticoSesion, Nivel, User
from services import neo4j_service
from services.progress_service import nivelar_unidad, NIVEL_MAX_DIFICULTAD

router = APIRouter(prefix="/ruta", tags=["ruta"])

log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────

def _unidad_num(unidad_id: str) -> int:
    try:
        return int(unidad_id.split("_")[1])
    except (IndexError, ValueError):
        raise HTTPException(400, f"unidad_id inválido: {unidad_id}")


def _nivel_diagnostico(unidad_id: str, student_id: int, db: Session) -> Nivel:
    sesion = (
        db.query(DiagnosticoSesion)
        .filter(
            DiagnosticoSesion.student_id == student_id,
            DiagnosticoSesion.unidad_id == unidad_id,
            DiagnosticoSesion.estado == "completado",
        )
        .order_by(DiagnosticoSesion.fecha_fin.desc())
        .first()
    )
    if sesion and sesion.nivel_resultado_global:
        return sesion.nivel_resultado_global
    return Nivel.BASICO


def _estado_diagnostico(unidad_id: str, student_id: int, db: Session) -> str:
    """'no_iniciado' | 'en_progreso' | 'completado' para esta unidad."""
    sesion = (
        db.query(DiagnosticoSesion)
        .filter(
            DiagnosticoSesion.student_id == student_id,
            DiagnosticoSesion.unidad_id == unidad_id,
        )
        .order_by(DiagnosticoSesion.fecha_inicio.desc())
        .first()
    )
    return sesion.estado if sesion else "no_iniciado"


def _topo_sort(nodos: list[dict], local_prereqs: dict[str, list[str]]) -> list[dict]:
    """Ordenamiento topológico de Kahn. Nodos sin prereqs van primero."""
    node_map = {n["tema_canonico"]: n for n in nodos}
    node_ids = set(node_map)
    in_deg = {n: 0 for n in node_ids}
    deps: dict[str, list[str]] = {}
    for nid in node_ids:
        lp = [p for p in local_prereqs.get(nid, []) if p in node_ids]
        deps[nid] = lp
        in_deg[nid] = len(lp)
    queue = sorted(n for n in node_ids if in_deg[n] == 0)
    result: list[dict] = []
    while queue:
        nid = queue.pop(0)
        result.append(node_map[nid])
        for other in sorted(node_ids):
            if nid in deps.get(other, []):
                in_deg[other] -= 1
                if in_deg[other] == 0:
                    queue.append(other)
                    queue.sort()
    done = {n["tema_canonico"] for n in result}
    for nid, n in node_map.items():
        if nid not in done:
            result.append(n)
    return result


def _get_dominados(student_id: int, db: Session) -> set[str]:
    return {
        p.node_id
        for p in db.query(StudentProgress)
        .filter(StudentProgress.student_id == student_id, StudentProgress.dominado == True)
        .all()
    }


# ── Schemas ──────────────────────────────────────────────────────

class MarcarDominadoRequest(BaseModel):
    node_id: str
    p_dominio: float


class NivelarUnidadRequest(BaseModel):
    unidad_id: str
    nivel: str


# ── Endpoints ────────────────────────────────────────────────────

@router.get("/unidad/{unidad_id}")
def get_ruta_unidad(
    unidad_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unidad_num = _unidad_num(unidad_id)
    nivel = _nivel_diagnostico(unidad_id, current_user.id, db)
    dificultad = NIVEL_MAX_DIFICULTAD[nivel.value]

    nodos = neo4j_service.get_nodos_unidad_nivel(unidad_num, dificultad)
    if not nodos:
        return {"unidad_id": unidad_id, "nivel_estudiante": nivel.value, "nodos": [], "total": 0, "dominados": 0}

    node_ids = [n["tema_canonico"] for n in nodos]
    node_ids_set = set(node_ids)

    all_prereqs = neo4j_service.get_prereqs_nodos(node_ids)
    local_prereqs = {
        tc: [p for p in prereqs if p in node_ids_set]
        for tc, prereqs in all_prereqs.items()
    }

    nodos_ordenados = _topo_sort(nodos, local_prereqs)
    dominados_set = _get_dominados(current_user.id, db)

    resultado = []
    for nodo in nodos_ordenados:
        tc = nodo["tema_canonico"]
        # Solo prerrequisitos visibles para el estudiante (misma unidad + nivel filtrado)
        visible_reqs = local_prereqs.get(tc, [])
        if tc in dominados_set:
            estado = "dominado"
        elif all(p in dominados_set for p in visible_reqs):
            estado = "siguiente"
        else:
            estado = "bloqueado"
        resultado.append({
            "node_id": tc,
            "nombre": nodo.get("nombre_display", tc),
            "descripcion": nodo.get("descripcion", ""),
            "dificultad": nodo.get("dificultad", 1),
            "tiempo_horas": nodo.get("tiempo_horas", ""),
            "estado": estado,
            "prereqs": visible_reqs,
        })

    return {
        "unidad_id": unidad_id,
        "nivel_estudiante": nivel.value,
        "nodos": resultado,
        "total": len(resultado),
        "dominados": sum(1 for n in resultado if n["estado"] == "dominado"),
    }


@router.get("/siguiente")
def get_siguiente_nodo(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unidad_id = f"unidad_{current_user.unidad_actual}"
    unidad_num = current_user.unidad_actual
    nivel = _nivel_diagnostico(unidad_id, current_user.id, db)
    dificultad = NIVEL_MAX_DIFICULTAD[nivel.value]

    nodos = neo4j_service.get_nodos_unidad_nivel(unidad_num, dificultad)
    if not nodos:
        return {"siguiente": None}

    node_ids = [n["tema_canonico"] for n in nodos]
    node_ids_set = set(node_ids)
    all_prereqs = neo4j_service.get_prereqs_nodos(node_ids)
    local_prereqs = {tc: [p for p in all_prereqs.get(tc, []) if p in node_ids_set] for tc in node_ids}
    nodos_ordenados = _topo_sort(nodos, local_prereqs)
    dominados_set = _get_dominados(current_user.id, db)

    for nodo in nodos_ordenados:
        tc = nodo["tema_canonico"]
        if tc in dominados_set:
            continue
        if all(p in dominados_set for p in local_prereqs.get(tc, [])):
            return {
                "siguiente": {
                    "node_id": tc,
                    "nombre": nodo.get("nombre_display", tc),
                    "unidad_id": unidad_id,
                }
            }
    return {"siguiente": None}


@router.get("/prerequisitos/{node_id}")
def get_prerequisitos(
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    prereqs = neo4j_service.get_prereqs_nodos([node_id]).get(node_id, [])
    if not prereqs:
        return {"node_id": node_id, "faltantes": [], "refuerzo_previo": [], "requiere_contenido": []}

    progress_map = {
        p.node_id: p
        for p in db.query(StudentProgress).filter(
            StudentProgress.student_id == current_user.id,
            StudentProgress.node_id.in_(prereqs),
        ).all()
    }
    dominados_set = {nid for nid, p in progress_map.items() if p.dominado}
    info_map = neo4j_service.get_nodos_info(prereqs)

    faltantes = [p for p in prereqs if p not in dominados_set]
    refuerzo_previo = [
        {"node_id": p, "nombre": info_map.get(p, {}).get("nombre_display", p)}
        for p in faltantes if p in progress_map
    ]
    requiere_contenido = [
        {"node_id": p, "nombre": info_map.get(p, {}).get("nombre_display", p)}
        for p in faltantes if p not in progress_map
    ]

    return {
        "node_id": node_id,
        "faltantes": faltantes,
        "refuerzo_previo": refuerzo_previo,
        "requiere_contenido": requiere_contenido,
    }


@router.post("/marcar-dominado")
def marcar_dominado(
    req: MarcarDominadoRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    progress = db.query(StudentProgress).filter(
        StudentProgress.student_id == current_user.id,
        StudentProgress.node_id == req.node_id,
    ).first()

    nuevo_dominado = req.p_dominio >= 0.75

    if progress:
        progress.p_dominio = req.p_dominio
        progress.dominado = nuevo_dominado
        if nuevo_dominado and not progress.nivel_confirmado:
            progress.nivel_confirmado = current_user.nivel_actual
    else:
        db.add(StudentProgress(
            student_id=current_user.id,
            node_id=req.node_id,
            p_dominio=req.p_dominio,
            dominado=nuevo_dominado,
            nivel_confirmado=current_user.nivel_actual if nuevo_dominado else None,
        ))

    db.commit()
    return {"node_id": req.node_id, "dominado": nuevo_dominado, "p_dominio": req.p_dominio}


@router.get("/progreso-completo")
def get_progreso_completo(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    resultado = []
    for u_num in [1, 2, 3]:
        unidad_id = f"unidad_{u_num}"
        nivel = _nivel_diagnostico(unidad_id, current_user.id, db)
        estado_diag = _estado_diagnostico(unidad_id, current_user.id, db)
        dificultad = NIVEL_MAX_DIFICULTAD[nivel.value]

        nodos = neo4j_service.get_nodos_unidad_nivel(u_num, dificultad)
        node_ids = [n["tema_canonico"] for n in nodos]
        total = len(node_ids)

        dominados_count = (
            db.query(StudentProgress)
            .filter(
                StudentProgress.student_id == current_user.id,
                StudentProgress.node_id.in_(node_ids),
                StudentProgress.dominado == True,
            )
            .count()
            if total > 0 else 0
        )

        resultado.append({
            "unidad_id": unidad_id,
            "nivel_diagnostico": nivel.value,
            "diagnostico_estado": estado_diag,
            "total_nodos": total,
            "nodos_dominados": dominados_count,
            "porcentaje": round(dominados_count / total * 100) if total > 0 else 0,
        })

    return {"unidades": resultado}


@router.post("/nivelar-unidad-anterior")
def nivelar_unidad_anterior(
    req: NivelarUnidadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    unidad_num = _unidad_num(req.unidad_id)
    if unidad_num <= 1:
        raise HTTPException(400, "No hay unidad anterior para unidad_1")

    unidad_anterior_id = f"unidad_{unidad_num - 1}"
    n = nivelar_unidad(unidad_anterior_id, req.nivel, current_user.id, db)
    return {"nodos_marcados": n, "unidad_anterior": unidad_anterior_id, "nivel": req.nivel}
