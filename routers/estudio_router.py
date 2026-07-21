import os
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from openai import OpenAI
from pinecone import Pinecone

from database import get_db
from auth import get_current_user
from models import StudentProgress, TutorMensaje, User
from services import neo4j_service
from services.progress_service import NIVEL_MAX_DIFICULTAD
from tutor.graph import _split_node_id

router = APIRouter(prefix="/estudio", tags=["estudio"])
log = logging.getLogger(__name__)

PINECONE_INDEX = "tutor-logica-computacional"
EMBEDDING_MODEL = "text-embedding-3-small"


# ── schemas ──────────────────────────────────────────────────────────

class EvaluarEjercicioRequest(BaseModel):
    node_id: str
    enunciado: str
    respuesta_estudiante: str

class GenerarQuizRequest(BaseModel):
    node_id: str

class EvaluarQuizRequest(BaseModel):
    node_id: str
    pregunta: str
    respuesta_estudiante: str

class ChatBurbujaRequest(BaseModel):
    node_id: str
    mensaje: str
    historial: list[dict] = []

class ChatUnidadRequest(BaseModel):
    unidad_id: str
    mensaje: str
    historial: list[dict] = []


NIVEL_TXT = {
    "BASICO": "Usa lenguaje muy simple y ejemplos cotidianos. El estudiante es principiante.",
    "MEDIO":  "Usa lenguaje técnico moderado con ejemplos de programación.",
    "ALTO":   "Puedes usar terminología avanzada y profundizar en detalles.",
}


# ── helpers ──────────────────────────────────────────────────────────

def _openai() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def _pinecone_index():
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    return pc.Index(PINECONE_INDEX)

def _nombre_tema(node_id: str) -> str:
    info = neo4j_service.get_nodos_info([node_id])
    return info.get(node_id, {}).get("nombre_display", node_id)

def _fetch_chunk_contenido(index, chunk_ids: list[str]) -> dict[str, str]:
    """Fetch contenido de Pinecone por chunk_id. Retorna {chunk_id: contenido}."""
    if not chunk_ids:
        return {}
    result = index.fetch(ids=chunk_ids)
    out = {}
    for cid, vec in (result.vectors or {}).items():
        contenido = (vec.metadata or {}).get("contenido", "")
        if contenido:
            out[cid] = contenido
    return out

def _buscar_ejemplo(index, base_id: str, dif: int, client: OpenAI) -> str | None:
    """Busca el chunk más relevante como ejemplo para el tema."""
    vec = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input="ejemplo resuelto paso a paso",
    ).data[0].embedding

    res = index.query(
        vector=vec,
        top_k=5,
        filter={"tema_canonico": {"$eq": base_id}, "dificultad": {"$eq": dif}},
        include_metadata=True,
    )
    # Prefiere ejemplo_resuelto, si no teoria
    for tipo_pref in ("ejemplo_resuelto", "teoria"):
        for m in res.matches:
            meta = m.metadata or {}
            if meta.get("tipo") == tipo_pref and meta.get("contenido"):
                return meta["contenido"]
    # Fallback: primer resultado con contenido
    for m in res.matches:
        c = (m.metadata or {}).get("contenido", "")
        if c:
            return c
    return None

def _get_todos_chunks_contenido(index, base_id: str, dif: int, client: OpenAI) -> list[str]:
    """Recupera contenido de todos los chunks del tema para usar como contexto."""
    vec = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=f"contenido del tema {base_id}",
    ).data[0].embedding

    res = index.query(
        vector=vec,
        top_k=10,
        filter={"tema_canonico": {"$eq": base_id}, "dificultad": {"$eq": dif}},
        include_metadata=True,
    )
    return [
        (m.metadata or {}).get("contenido", "")
        for m in res.matches
        if (m.metadata or {}).get("contenido")
    ]

def _contexto_str(contenidos: list[str]) -> str:
    return "\n\n".join(c for c in contenidos if c)

def _generar_con_llm(client: OpenAI, nombre: str, tipo: str, contexto: str) -> str:
    """Usa GPT-4o para generar contenido pedagógico limpio a partir del chunk."""
    prompts = {
        "definicion": (
            f"Eres un tutor de lógica computacional. Explica de forma clara y directa qué es '{nombre}'.\n"
            f"Basa tu explicación en este contenido del libro:\n\n{contexto}\n\n"
            "Escribe una definición clara en 3-5 oraciones. "
            "No menciones figuras, páginas ni referencias bibliográficas. "
            "Usa lenguaje sencillo dirigido a un estudiante universitario de primer semestre."
        ),
        "ejemplo": (
            f"Eres un tutor de lógica computacional. Presenta un ejemplo concreto sobre '{nombre}'.\n"
            f"Basa el ejemplo en este contenido del libro:\n\n{contexto}\n\n"
            "Muestra un ejemplo paso a paso que ilustre el concepto. "
            "No menciones figuras, páginas ni referencias. "
            "Si el contenido incluye pseudocódigo o pasos, preséntalos de forma ordenada y clara."
        ),
        "ejercicio": (
            f"Eres un tutor de lógica computacional. Formula un ejercicio práctico sobre '{nombre}'.\n"
            f"Basa el ejercicio en este contenido del libro:\n\n{contexto}\n\n"
            "Escribe el enunciado de un ejercicio que el estudiante deba resolver. "
            "No menciones figuras ni páginas. "
            "El ejercicio debe ser respondible en texto, sin necesidad de dibujar diagramas."
        ),
    }

    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.4,
        messages=[
            {"role": "system", "content": prompts[tipo]},
            {"role": "user", "content": "Genera el contenido."},
        ],
    )
    return resp.choices[0].message.content.strip()


# ── endpoints ────────────────────────────────────────────────────────

@router.get("/contenido/{node_id}")
def get_contenido(
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    info = neo4j_service.get_nodos_info([node_id])
    if not info:
        raise HTTPException(404, f"Tema {node_id} no encontrado")

    nombre = info[node_id].get("nombre_display", node_id)
    base_id, dif = _split_node_id(node_id)

    unidad_str = info[node_id].get("unidad", "Unidad 1")
    try:
        unidad_id = f"unidad_{int(unidad_str.split()[-1])}"
    except (ValueError, IndexError):
        unidad_id = "unidad_1"

    por_tipo = neo4j_service.get_chunk_ids_por_tipo(node_id)
    index = _pinecone_index()
    client = _openai()

    # Reunir contexto crudo por tipo
    def_ids = por_tipo.get("definicion", [])
    def_raw = None
    if def_ids:
        fetched = _fetch_chunk_contenido(index, def_ids[:3])
        def_raw = next((fetched[c] for c in def_ids if fetched.get(c)), None)

    # Para ejemplo: búsqueda semántica
    ej_raw = _buscar_ejemplo(index, base_id, dif, client)

    en_ids = por_tipo.get("enunciado", [])
    en_raw = None
    if en_ids:
        fetched2 = _fetch_chunk_contenido(index, en_ids[:2])
        en_raw = next((fetched2[c] for c in en_ids if fetched2.get(c)), None)

    # Pasar por LLM para generar contenido pedagógico limpio
    definicion_texto = _generar_con_llm(client, nombre, "definicion", def_raw or ej_raw or "") if (def_raw or ej_raw) else None
    ejemplo_texto    = _generar_con_llm(client, nombre, "ejemplo",    ej_raw  or def_raw or "") if (ej_raw or def_raw) else None
    ejercicio_texto  = _generar_con_llm(client, nombre, "ejercicio",  en_raw  or def_raw or ej_raw or "") if (en_raw or def_raw or ej_raw) else None

    prog = db.query(StudentProgress).filter(
        StudentProgress.student_id == current_user.id,
        StudentProgress.node_id == node_id,
    ).first()

    return {
        "node_id": node_id,
        "nombre": nombre,
        "unidad_id": unidad_id,
        "dominado": prog.dominado if prog else False,
        "definicion": {"contenido": definicion_texto} if definicion_texto else None,
        "ejemplo":    {"contenido": ejemplo_texto}    if ejemplo_texto    else None,
        "enunciado":  {"contenido": ejercicio_texto}  if ejercicio_texto  else None,
    }


@router.post("/evaluar-ejercicio")
def evaluar_ejercicio(
    req: EvaluarEjercicioRequest,
    current_user: User = Depends(get_current_user),
):
    base_id, dif = _split_node_id(req.node_id)
    index = _pinecone_index()
    client = _openai()

    contenidos = _get_todos_chunks_contenido(index, base_id, dif, client)
    if not contenidos:
        raise HTTPException(404, f"Sin contenido para {req.node_id}")

    nombre = _nombre_tema(req.node_id)
    contexto = _contexto_str(contenidos)

    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.3,
        messages=[
            {
                "role": "system",
                "content": (
                    f"Eres un tutor de lógica computacional evaluando un ejercicio sobre '{nombre}'.\n"
                    f"Usa este contenido como referencia:\n\n{contexto}\n\n"
                    "Evalúa la respuesta del estudiante de forma constructiva:\n"
                    "- Correcta: confirma y refuerza el concepto clave.\n"
                    "- Parcial: señala qué está bien y qué falta.\n"
                    "- Incorrecta: explica el error sin dar la respuesta completa.\n"
                    "Máximo 3 oraciones. Termina con exactamente una de estas palabras en mayúsculas: CORRECTO, PARCIAL, INCORRECTO."
                ),
            },
            {
                "role": "user",
                "content": f"Ejercicio: {req.enunciado}\n\nRespuesta: {req.respuesta_estudiante}",
            },
        ],
    )

    feedback = resp.choices[0].message.content.strip()
    texto_upper = feedback.upper()
    correcto = "CORRECTO" in texto_upper and "INCORRECTO" not in texto_upper

    return {"feedback": feedback, "correcto": correcto}


@router.post("/generar-quiz")
def generar_quiz(
    req: GenerarQuizRequest,
    current_user: User = Depends(get_current_user),
):
    base_id, dif = _split_node_id(req.node_id)
    index = _pinecone_index()
    client = _openai()

    contenidos = _get_todos_chunks_contenido(index, base_id, dif, client)
    if not contenidos:
        raise HTTPException(404, f"Sin contenido para {req.node_id}")

    nombre = _nombre_tema(req.node_id)
    contexto = _contexto_str(contenidos[:5])

    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.7,
        messages=[
            {
                "role": "system",
                "content": (
                    f"Eres un tutor de lógica computacional. Genera UNA pregunta de quiz sobre '{nombre}' "
                    f"basada estrictamente en este contenido:\n\n{contexto}\n\n"
                    "La pregunta debe evaluar comprensión conceptual, no memorización, "
                    "y ser respondible en 1-3 oraciones. "
                    "Responde SOLO con la pregunta, sin explicaciones."
                ),
            },
            {"role": "user", "content": "Genera la pregunta."},
        ],
    )

    return {"pregunta": resp.choices[0].message.content.strip(), "node_id": req.node_id}


@router.post("/evaluar-quiz")
def evaluar_quiz(
    req: EvaluarQuizRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    base_id, dif = _split_node_id(req.node_id)
    index = _pinecone_index()
    client = _openai()

    contenidos = _get_todos_chunks_contenido(index, base_id, dif, client)
    if not contenidos:
        raise HTTPException(404, f"Sin contenido para {req.node_id}")

    nombre = _nombre_tema(req.node_id)
    contexto = _contexto_str(contenidos[:5])

    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    f"Eres un tutor evaluando si el estudiante dominó '{nombre}'.\n"
                    f"Referencia:\n\n{contexto}\n\n"
                    "Evalúa si la respuesta demuestra comprensión del concepto.\n"
                    "Responde en este formato exacto:\n"
                    "RESULTADO: APROBADO o REPROBADO\n"
                    "FEEDBACK: (una o dos oraciones de retroalimentación)"
                ),
            },
            {
                "role": "user",
                "content": f"Pregunta: {req.pregunta}\n\nRespuesta: {req.respuesta_estudiante}",
            },
        ],
    )

    content = resp.choices[0].message.content.strip()
    aprobado = "APROBADO" in content.upper()

    feedback_line = next(
        (l.replace("FEEDBACK:", "").strip() for l in content.split("\n") if l.startswith("FEEDBACK:")),
        content,
    )

    if aprobado:
        prog = db.query(StudentProgress).filter(
            StudentProgress.student_id == current_user.id,
            StudentProgress.node_id == req.node_id,
        ).first()
        if prog:
            prog.dominado = True
            prog.p_dominio = 0.9
        else:
            db.add(StudentProgress(
                student_id=current_user.id,
                node_id=req.node_id,
                p_dominio=0.9,
                dominado=True,
            ))
        db.commit()

    node_id_siguiente = None
    if aprobado:
        from routers.tutor_router import _nivel_estudiante, _siguiente_nodo
        nivel = _nivel_estudiante(req.node_id, current_user.id, db)
        try:
            node_id_siguiente = _siguiente_nodo(req.node_id, nivel, current_user.id, db)
        except Exception as exc:
            log.warning(f"siguiente_nodo error: {exc}")

    return {
        "aprobado": aprobado,
        "feedback": feedback_line,
        "node_id_siguiente": node_id_siguiente,
    }


@router.get("/chat-historial")
def get_chat_historial(
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    mensajes = (
        db.query(TutorMensaje)
        .filter(TutorMensaje.student_id == current_user.id, TutorMensaje.node_id == node_id)
        .order_by(TutorMensaje.timestamp.asc())
        .all()
    )
    return [{"rol": m.rol, "contenido": m.contenido} for m in mensajes]


@router.post("/chat-burbuja")
def chat_burbuja(
    req: ChatBurbujaRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    nombre = _nombre_tema(req.node_id)
    base_id, dif = _split_node_id(req.node_id)

    client = _openai()
    index = _pinecone_index()

    vec = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=req.mensaje,
    ).data[0].embedding

    res = index.query(
        vector=vec,
        top_k=3,
        filter={"tema_canonico": {"$eq": base_id}, "dificultad": {"$eq": dif}},
        include_metadata=True,
    )

    contexto = "\n\n".join(
        m.metadata.get("contenido", "") for m in res.matches if m.metadata and m.metadata.get("contenido")
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"Eres un tutor de lógica computacional. El estudiante está estudiando '{nombre}'.\n"
                f"Responde usando este contenido como referencia:\n\n{contexto}\n\n"
                "REGLAS:\n"
                "- Solo responde preguntas sobre este tema específico.\n"
                "- Si preguntan sobre el ejercicio propuesto o el quiz, responde: "
                "'Eso debes resolverlo tú. ¿Tienes alguna duda sobre el concepto?'\n"
                "- Si la pregunta no es sobre el tema, redirige amablemente.\n"
                "- Respuestas cortas y claras, máximo 4 oraciones."
            ),
        }
    ]

    for h in req.historial[-6:]:
        messages.append({"role": h["rol"], "content": h["contenido"]})
    messages.append({"role": "user", "content": req.mensaje})

    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.4,
        messages=messages,
    )
    respuesta = resp.choices[0].message.content.strip()

    db.add(TutorMensaje(student_id=current_user.id, node_id=req.node_id, rol="user", contenido=req.mensaje))
    db.add(TutorMensaje(student_id=current_user.id, node_id=req.node_id, rol="assistant", contenido=respuesta))
    db.commit()

    return {"respuesta": respuesta}


@router.post("/chat-unidad")
def chat_unidad(
    req: ChatUnidadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from routers.ruta_router import _nivel_diagnostico, _unidad_num

    unidad_num = _unidad_num(req.unidad_id)
    nivel = _nivel_diagnostico(req.unidad_id, current_user.id, db).value
    max_dif = NIVEL_MAX_DIFICULTAD[nivel]

    nodos = neo4j_service.get_nodos_unidad(unidad_num, max_dif)
    node_ids = [n["tema_canonico"] for n in nodos]
    if not node_ids:
        raise HTTPException(404, f"Sin contenido para {req.unidad_id}")

    client = _openai()
    index = _pinecone_index()

    vec = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=req.mensaje,
    ).data[0].embedding

    res = index.query(
        vector=vec,
        top_k=5,
        filter={"tema_canonico": {"$in": node_ids}, "dificultad": {"$lte": max_dif}},
        include_metadata=True,
    )

    contexto = "\n\n".join(
        m.metadata.get("contenido", "") for m in res.matches if m.metadata and m.metadata.get("contenido")
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"Eres un tutor de lógica computacional. El estudiante está en la unidad {unidad_num} "
                "y puede preguntar sobre cualquier tema de esta unidad, no solo uno en particular.\n"
                f"Nivel del estudiante: {nivel}. {NIVEL_TXT.get(nivel, '')}\n"
                f"Responde usando este contenido como referencia:\n\n{contexto}\n\n"
                "REGLAS:\n"
                "- Solo responde preguntas relacionadas con los temas de esta unidad.\n"
                "- Si la pregunta no es sobre la unidad, redirige amablemente.\n"
                "- Respuestas cortas y claras, máximo 4 oraciones."
            ),
        }
    ]

    for h in req.historial[-6:]:
        messages.append({"role": h["rol"], "content": h["contenido"]})
    messages.append({"role": "user", "content": req.mensaje})

    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.4,
        messages=messages,
    )
    respuesta = resp.choices[0].message.content.strip()

    db.add(TutorMensaje(student_id=current_user.id, node_id=req.unidad_id, rol="user", contenido=req.mensaje))
    db.add(TutorMensaje(student_id=current_user.id, node_id=req.unidad_id, rol="assistant", contenido=respuesta))
    db.commit()

    return {"respuesta": respuesta}
