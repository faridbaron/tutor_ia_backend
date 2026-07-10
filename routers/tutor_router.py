import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from models import StudentProgress, TutorMensaje, DiagnosticoSesion, Nivel, User
from services import neo4j_service
from services.progress_service import NIVEL_MAX_DIFICULTAD
from tutor.graph import run_turn

router = APIRouter(prefix="/tutor", tags=["tutor"])
log = logging.getLogger(__name__)


# ── schemas ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    node_id:  str
    mensaje:  str = ""
    historial: list[dict] = []


# ── helpers ──────────────────────────────────────────────────────────

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


def _p_dominio_actual(node_id: str, student_id: int, db: Session) -> float:
    p = db.query(StudentProgress).filter(
        StudentProgress.student_id == student_id,
        StudentProgress.node_id    == node_id,
    ).first()
    return p.p_dominio if p else 0.2


def _prereqs_faltantes(node_id: str, student_id: int, nivel: str, db: Session) -> list[str]:
    prereqs = neo4j_service.get_prereqs_nodos([node_id]).get(node_id, [])
    if not prereqs:
        return []

    # Only consider prereqs visible at the student's level (same unit, within max_dificultad)
    info = neo4j_service.get_nodos_info([node_id])
    unidad_str = info.get(node_id, {}).get("unidad", "UNIDAD 1")
    try:
        unidad_num = int(unidad_str.split()[-1])
    except (ValueError, IndexError):
        unidad_num = 1
    max_dif = NIVEL_MAX_DIFICULTAD[nivel]
    nodos_visibles = {n["tema_canonico"] for n in neo4j_service.get_nodos_unidad(unidad_num, max_dif)}
    prereqs_visibles = [p for p in prereqs if p in nodos_visibles]

    if not prereqs_visibles:
        return []
    dominados = {
        r.node_id
        for r in db.query(StudentProgress).filter(
            StudentProgress.student_id == student_id,
            StudentProgress.node_id.in_(prereqs_visibles),
            StudentProgress.dominado   == True,
        ).all()
    }
    return [p for p in prereqs_visibles if p not in dominados]


def _siguiente_nodo(node_id: str, nivel: str, student_id: int, db: Session):
    info = neo4j_service.get_nodos_info([node_id])
    unidad_str = info.get(node_id, {}).get("unidad", "UNIDAD 1")
    try:
        unidad_num = int(unidad_str.split()[-1])
    except (ValueError, IndexError):
        unidad_num = 1
    max_dif = NIVEL_MAX_DIFICULTAD[nivel]
    nodos   = neo4j_service.get_nodos_unidad(unidad_num, max_dif)
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


# ── endpoints ────────────────────────────────────────────────────────

@router.post("/chat")
def chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    nivel    = _nivel_estudiante(req.node_id, current_user.id, db)
    p_actual = _p_dominio_actual(req.node_id, current_user.id, db)

    # Si el nodo ya está dominado (repaso), no bloquear por prereqs
    ya_dominado = p_actual >= 0.75
    faltantes = [] if ya_dominado else _prereqs_faltantes(req.node_id, current_user.id, nivel, db)

    result = run_turn(
        student_id        = current_user.id,
        node_id           = req.node_id,
        mensaje           = req.mensaje,
        historial         = req.historial,
        nivel             = nivel,
        p_dominio         = p_actual,
        prereqs_faltantes = faltantes,
    )

    respuesta     = result.get("respuesta", "")
    tipo          = result.get("tipo_respuesta", "pregunta")
    p_nuevo       = result.get("p_dominio_nuevo", p_actual)
    sugerencia    = result.get("sugerencia", "continuar")
    chunks_usados = result.get("chunks_usados", [])

    if tipo != "prereq":
        if req.mensaje.strip():
            db.add(TutorMensaje(
                student_id       = current_user.id,
                node_id          = req.node_id,
                rol              = "user",
                contenido        = req.mensaje,
                tipo_respuesta   = tipo,
                p_dominio_momento = p_actual,
            ))
        db.add(TutorMensaje(
            student_id        = current_user.id,
            node_id           = req.node_id,
            rol               = "assistant",
            contenido         = respuesta,
            tipo_respuesta    = tipo,
            p_dominio_momento = p_nuevo,
        ))

        prog = db.query(StudentProgress).filter(
            StudentProgress.student_id == current_user.id,
            StudentProgress.node_id    == req.node_id,
        ).first()
        dominado_nuevo = p_nuevo >= 0.75
        if prog:
            if prog.dominado:
                # Ya estaba dominado: el chat (repaso/preguntas) no debe hacer
                # retroceder un progreso ya alcanzado.
                prog.p_dominio = max(p_nuevo, prog.p_dominio)
            else:
                prog.p_dominio = p_nuevo
                prog.dominado  = dominado_nuevo
        else:
            db.add(StudentProgress(
                student_id = current_user.id,
                node_id    = req.node_id,
                p_dominio  = p_nuevo,
                dominado   = dominado_nuevo,
            ))
        db.commit()

    node_id_siguiente = None
    if sugerencia == "siguiente_nodo":
        try:
            node_id_siguiente = _siguiente_nodo(req.node_id, nivel, current_user.id, db)
        except Exception as exc:
            log.warning(f"siguiente_nodo error: {exc}")

    return {
        "respuesta":       respuesta,
        "tipo_respuesta":  tipo,
        "chunks_usados":   chunks_usados,
        "p_dominio":       p_nuevo,
        "sugerencia":      sugerencia,
        "node_id_siguiente": node_id_siguiente,
    }


@router.get("/contexto/{node_id}")
def get_contexto(
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        import os
        from openai import OpenAI
        from pinecone import Pinecone

        info   = neo4j_service.get_nodos_info([node_id])
        nombre = info.get(node_id, {}).get("nombre_display", node_id)

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        pc     = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        index  = pc.Index("tutor-logica-computacional")

        vec = client.embeddings.create(
            model="text-embedding-3-small",
            input=f"Tema: {node_id} definicion {nombre}",
        ).data[0].embedding

        from tutor.graph import _split_node_id
        base_id, dif = _split_node_id(node_id)
        res = index.query(
            vector=vec, top_k=5,
            filter={"tema_canonico": {"$eq": base_id}, "dificultad": {"$eq": dif}},
            include_metadata=True,
        )
        priority = {"definicion": 0, "ejemplo_resuelto": 1, "enunciado": 2}
        chunks = sorted(
            [
                {
                    "chunk_id": (m.metadata or {}).get("chunk_id", m.id),
                    "tipo":     (m.metadata or {}).get("tipo", ""),
                    "contenido": (m.metadata or {}).get("contenido", ""),
                }
                for m in res.matches
            ],
            key=lambda x: priority.get(x["tipo"], 99),
        )[:3]

        return {"node_id": node_id, "nombre": nombre, "chunks": chunks}
    except Exception as exc:
        log.error(f"contexto error: {exc}")
        raise HTTPException(500, f"Error cargando contexto: {exc}")


@router.get("/sesion/{node_id}")
def get_sesion(
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    mensajes = (
        db.query(TutorMensaje)
        .filter(
            TutorMensaje.student_id == current_user.id,
            TutorMensaje.node_id    == node_id,
        )
        .order_by(TutorMensaje.timestamp.asc())
        .all()
    )
    prog = db.query(StudentProgress).filter(
        StudentProgress.student_id == current_user.id,
        StudentProgress.node_id    == node_id,
    ).first()

    return {
        "node_id": node_id,
        "historial": [
            {"rol": m.rol, "contenido": m.contenido, "tipo_respuesta": m.tipo_respuesta}
            for m in mensajes
        ],
        "p_dominio": prog.p_dominio if prog else 0.0,
        "dominado":  prog.dominado  if prog else False,
    }
