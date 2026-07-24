import os
import logging
from typing import TypedDict, Optional
from openai import OpenAI
from pinecone import Pinecone
from langgraph.graph import StateGraph, END

from services import neo4j_service

log = logging.getLogger(__name__)

# BKT parameters for chat-based learning
P_LEARN  = 0.30
P_GUESS  = 0.20
P_SLIP   = 0.10
P_FORGET = 0.05

PINECONE_INDEX  = "tutor-logica-computacional"
EMBEDDING_MODEL = "text-embedding-3-small"
TIPO_PRIORITY   = {"definicion": 0, "ejemplo_resuelto": 1, "enunciado": 2}


class TutorState(TypedDict, total=False):
    # Set by router
    student_id:        int
    node_id:           str
    mensaje:           str
    historial:         list        # [{rol, contenido}]
    nivel:             str         # BASICO|MEDIO|ALTO
    p_dominio:         float
    prereqs_faltantes: list        # node_ids of missing prereqs

    # Computed by graph
    nombre_tema:       str
    prereq_aviso:      str         # display names of missing prereqs (soft warning)
    chunks:            list        # [{chunk_id, tipo, contenido, score}]
    tipo_respuesta:    str         # correcto|no_entiende|error_comun|pregunta|inicial
    respuesta:         str
    chunks_usados:     list
    p_dominio_nuevo:   float
    sugerencia:        str         # continuar|reforzar|siguiente_nodo
    node_id_siguiente: Optional[str]


# ── lazy clients ─────────────────────────────────────────────────────

def _openai() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def _pinecone_index():
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    return pc.Index(PINECONE_INDEX)


# ── graph nodes ──────────────────────────────────────────────────────

def nodo_entrada(state: TutorState) -> dict:
    info = neo4j_service.get_nodos_info([state["node_id"]])
    nombre = info.get(state["node_id"], {}).get("nombre_display", state["node_id"])
    return {"nombre_tema": nombre}


def nodo_prerequisito(state: TutorState) -> dict:
    faltantes = state.get("prereqs_faltantes") or []
    if not faltantes:
        return {}
    info = neo4j_service.get_nodos_info(faltantes)
    nombres = ", ".join(
        info.get(p, {}).get("nombre_display", p) for p in faltantes[:3]
    )
    # Store display names for nodo_respuesta to include as a soft warning
    return {"prereq_aviso": nombres}


def _route_prereq(state: TutorState) -> str:
    return "retrieval"


def _split_node_id(node_id: str) -> tuple[str, int]:
    """'que_es_un_algoritmo_BASICO' → ('que_es_un_algoritmo', 1)"""
    SUF_DIF = {"BASICO": 1, "MEDIO": 2, "ALTO": 3}
    for suf, dif in SUF_DIF.items():
        if node_id.endswith(f"_{suf}"):
            return node_id[: -(len(suf) + 1)], dif
    return node_id, 1


def nodo_retrieval(state: TutorState) -> dict:
    try:
        client = _openai()
        index  = _pinecone_index()
        query  = f"Tema: {state['node_id']} {(state.get('mensaje') or state.get('nombre_tema', ''))}"
        vec    = client.embeddings.create(model=EMBEDDING_MODEL, input=query).data[0].embedding

        base_id, dif = _split_node_id(state["node_id"])
        res = index.query(
            vector=vec,
            top_k=6,
            filter={"tema_canonico": {"$eq": base_id}, "dificultad": {"$eq": dif}},
            include_metadata=True,
        )
        chunks = []
        for m in res.matches:
            meta = m.metadata or {}
            tipo = meta.get("tipo", "")
            chunks.append({
                "chunk_id": meta.get("chunk_id", m.id),
                "tipo":     tipo,
                "contenido": meta.get("contenido", ""),
                "score":    m.score,
                "priority": TIPO_PRIORITY.get(tipo, 99),
            })
        chunks.sort(key=lambda x: (x["priority"], -x["score"]))
        return {"chunks": chunks[:3]}
    except Exception as exc:
        log.warning(f"retrieval error: {exc}")
        return {"chunks": []}


def nodo_evaluacion(state: TutorState) -> dict:
    mensaje = (state.get("mensaje") or "").strip()
    if not mensaje:
        return {"tipo_respuesta": "inicial"}
    if not state.get("historial"):
        return {"tipo_respuesta": "pregunta"}

    try:
        hist_str = "\n".join(
            f"{'Tutor' if m['rol'] == 'assistant' else 'Estudiante'}: {m['contenido']}"
            for m in (state.get("historial") or [])[-4:]
        )
        resp = _openai().chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f'Clasifica la última respuesta del estudiante sobre '
                        f'"{state.get("nombre_tema", state["node_id"])}" en UNA palabra: '
                        "correcto, no_entiende, error_comun, pregunta. Solo esa palabra."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Historial:\n{hist_str}\n\nEstudiante ahora: {mensaje}",
                },
            ],
            temperature=0,
            max_tokens=5,
        )
        cat = resp.choices[0].message.content.strip().lower()
        if cat not in ("correcto", "no_entiende", "error_comun", "pregunta"):
            cat = "pregunta"
        return {"tipo_respuesta": cat}
    except Exception as exc:
        log.warning(f"evaluacion error: {exc}")
        return {"tipo_respuesta": "pregunta"}


def nodo_bkt(state: TutorState) -> dict:
    p    = state.get("p_dominio", 0.2)
    tipo = state.get("tipo_respuesta", "pregunta")

    if tipo in ("correcto", "no_entiende", "error_comun"):
        correcto = tipo == "correcto"
        if correcto:
            p_post = (p * (1 - P_SLIP)) / (p * (1 - P_SLIP) + (1 - p) * P_GUESS)
            p_new  = p_post + (1 - p_post) * P_LEARN
        else:
            p_post = (p * P_SLIP) / (p * P_SLIP + (1 - p) * (1 - P_GUESS))
            p_new  = p_post * (1 - P_FORGET)
        p = min(max(p_new, 0.0), 1.0)

    return {"p_dominio_nuevo": p}


def nodo_respuesta(state: TutorState) -> dict:
    nivel  = state.get("nivel", "BASICO")
    tipo   = state.get("tipo_respuesta", "inicial")
    chunks = state.get("chunks") or []
    nombre = state.get("nombre_tema", state["node_id"])

    contexto = "\n\n".join(
        f"[{c['tipo']}]: {c['contenido']}"
        for c in chunks if c.get("contenido")
    )

    nivel_txt = {
        "BASICO": "Usa lenguaje muy simple y ejemplos cotidianos. El estudiante es principiante.",
        "MEDIO":  "Usa lenguaje técnico moderado con ejemplos de programación.",
        "ALTO":   "Puedes usar terminología avanzada y profundizar en detalles.",
    }.get(nivel, "")

    tipo_txt = {
        "inicial":     f"Presenta '{nombre}' de forma motivadora. Haz UNA pregunta para activar conocimientos previos.",
        "correcto":    "Felicita brevemente al estudiante y avanza o profundiza en el concepto.",
        "no_entiende": "Da una PISTA GRADUAL sin revelar la solución: usa una analogía o ejemplo concreto.",
        "error_comun": "Identifica el error conceptual del estudiante y corrígelo con gentileza. Muestra el concepto correcto.",
        "pregunta":    "Responde la pregunta del estudiante de forma directa y clara.",
    }.get(tipo, "Responde de forma útil.")

    prereq_aviso = state.get("prereq_aviso", "")
    aviso_txt = (
        f"\n\n⚠️ Nota: el estudiante aún no ha completado: {prereq_aviso}. "
        "Menciona brevemente al inicio que sería ideal completarlos primero, pero igual preséntate y ofrece ayuda."
        if prereq_aviso else ""
    )

    system = (
        f"Eres un tutor de lógica computacional y programación en español.\n"
        f"Eres paciente, motivador y claro. Nivel del estudiante: {nivel}. {nivel_txt}\n"
        f"Tema actual: {nombre}\n"
        + (f"\nContexto del libro:\n{contexto}" if contexto
           else "\n(Contenido del libro no disponible; usa tu conocimiento general del tema.)")
        + f"\n\nInstrucción: {tipo_txt}"
        + aviso_txt
        + "\n\nSé conciso: máximo 3 párrafos cortos. No repitas el contexto textualmente."
        + "\n\nSi el contexto del libro no responde directamente lo que se pregunta, ignóralo "
          "y responde con tu propio conocimiento correcto del tema — no fuerces una respuesta "
          "basada en contexto que no aplica, y no menciones que el contexto no era relevante."
        + "\n\nNo uses LaTeX ni comandos como \\text, \\frac, \\times o \\log, ni delimitadores "
          "como [ ], $ o $$. Escribe cualquier fórmula en texto plano legible (por ejemplo: "
          "dB = 10 * log10(P_salida / P_entrada)) o dentro de un bloque de código."
    )

    messages = [{"role": "system", "content": system}]
    for m in (state.get("historial") or [])[-10:]:
        messages.append({"role": "user" if m["rol"] == "user" else "assistant", "content": m["contenido"]})
    if state.get("mensaje"):
        messages.append({"role": "user", "content": state["mensaje"]})

    try:
        resp = _openai().chat.completions.create(
            model="gpt-4o", messages=messages, temperature=0.7, max_tokens=600
        )
        return {
            "respuesta":    resp.choices[0].message.content,
            "chunks_usados": [c["chunk_id"] for c in chunks if c.get("chunk_id")],
        }
    except Exception as exc:
        log.error(f"respuesta error: {exc}")
        return {
            "respuesta":    "Hubo un error generando la respuesta. Intenta de nuevo.",
            "chunks_usados": [],
        }


def nodo_siguiente(state: TutorState) -> dict:
    p = state.get("p_dominio_nuevo", state.get("p_dominio", 0.2))
    if p >= 0.75:
        return {"sugerencia": "siguiente_nodo", "node_id_siguiente": None}
    elif p < 0.4:
        return {"sugerencia": "reforzar",       "node_id_siguiente": None}
    else:
        return {"sugerencia": "continuar",      "node_id_siguiente": None}


# ── compile graph ─────────────────────────────────────────────────────

def _build() -> object:
    g = StateGraph(TutorState)
    g.add_node("entrada",      nodo_entrada)
    g.add_node("prerequisito", nodo_prerequisito)
    g.add_node("retrieval",    nodo_retrieval)
    g.add_node("evaluacion",   nodo_evaluacion)
    g.add_node("bkt",          nodo_bkt)
    g.add_node("respuesta",    nodo_respuesta)
    g.add_node("siguiente",    nodo_siguiente)

    g.set_entry_point("entrada")
    g.add_edge("entrada",    "prerequisito")
    g.add_conditional_edges("prerequisito", _route_prereq)
    g.add_edge("retrieval",  "evaluacion")
    g.add_edge("evaluacion", "bkt")
    g.add_edge("bkt",        "respuesta")
    g.add_edge("respuesta",  "siguiente")
    g.add_edge("siguiente",  END)

    return g.compile()


tutor_graph = _build()


def run_turn(
    *,
    student_id: int,
    node_id: str,
    mensaje: str,
    historial: list,
    nivel: str,
    p_dominio: float,
    prereqs_faltantes: list,
) -> dict:
    """Public entry point called by the router."""
    initial: TutorState = {
        "student_id":        student_id,
        "node_id":           node_id,
        "mensaje":           mensaje,
        "historial":         historial[-10:],
        "nivel":             nivel,
        "p_dominio":         p_dominio,
        "prereqs_faltantes": prereqs_faltantes,
    }
    return tutor_graph.invoke(initial)
