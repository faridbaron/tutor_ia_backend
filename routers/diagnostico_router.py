import json
import logging
import random
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
import models
from models import DiagnosticoSesion, BKTEstado, Nivel, User
from services.progress_service import nivelar_unidad
from bkt.engine import (
    BKT_PARAMS, KC_ORDEN_POR_UNIDAD, NIVEL_ORDEN,
    DOMINIO_UMBRAL, DESCENSO_UMBRAL, MAX_PREGUNTAS_POR_KC, MIN_PREGUNTAS_POR_KC,
    actualizar_p_dominio, seleccionar_pregunta, nivel_global, nivel_promedio,
)

router = APIRouter(prefix="/diagnostico", tags=["diagnostico"])

# Carga los bancos de las tres unidades al arrancar
_DATA_DIR = Path(__file__).parent.parent / "data"
BANCO_POR_UNIDAD: dict[str, list] = {}
for _u in ["unidad_1", "unidad_2", "unidad_3"]:
    _fname = f"preguntas_{_u.replace('_', '')}.json"  # unidad_1 → preguntasunidad1? No
    # preguntas_unidad1.json: "unidad_" + "1" = "unidad_1" → strip "_" between "unidad" and digit
    # Filename pattern: preguntas_unidad1.json, preguntas_unidad2.json, preguntas_unidad3.json
    _fname = f"preguntas_unidad{_u.split('_')[1]}.json"
    _path = _DATA_DIR / _fname
    with open(_path, encoding="utf-8") as _f:
        BANCO_POR_UNIDAD[_u] = json.load(_f)

UNIDADES_VALIDAS = set(BANCO_POR_UNIDAD.keys())


# ── Schemas ─────────────────────────────────────────────────────

class IniciarRequest(BaseModel):
    unidad_id: str = "unidad_1"


class ResponderRequest(BaseModel):
    sesion_id: str
    pregunta_id: str
    respuesta: str  # "A" | "B" | "C" | "D"


# ── Helpers ─────────────────────────────────────────────────────

def _shuffle_opciones(p: dict):
    """Shuffle determinista usando pregunta_id como semilla.
    Retorna (opciones_barajadas, letter_map) donde letter_map[letra_barajada] = letra_original."""
    letras = ["A", "B", "C", "D"]
    indices = list(range(len(p["opciones"])))
    random.Random(p["pregunta_id"]).shuffle(indices)
    contenidos = [op[3:] for op in p["opciones"]]  # strip "X) "
    opciones_shuffled = [f"{letras[i]}) {contenidos[indices[i]]}" for i in range(len(indices))]
    letter_map = {letras[i]: letras[indices[i]] for i in range(len(indices))}
    return opciones_shuffled, letter_map


def _pregunta_publica(p: dict) -> dict:
    opciones_shuffled, _ = _shuffle_opciones(p)
    return {
        "pregunta_id": p["pregunta_id"],
        "enunciado": p["enunciado"],
        "opciones": opciones_shuffled,
        "dominio": p["dominio"],
        "nivel": p["nivel"],
    }


def _nivel_str(val) -> str:
    if hasattr(val, "value"):
        return val.value
    return str(val)


def _estados_ordenados(db: Session, sesion_id: str, kc_orden: list) -> list:
    estados = db.query(BKTEstado).filter(BKTEstado.sesion_id == sesion_id).all()
    mapa = {e.kc_dominio: e for e in estados}
    return [mapa[kc] for kc in kc_orden if kc in mapa]


def _siguiente_estado(estados: list) -> Optional[BKTEstado]:
    return next((e for e in estados if not e.completado), None)


def _kc_orden(unidad_id: str) -> list:
    return KC_ORDEN_POR_UNIDAD.get(unidad_id, KC_ORDEN_POR_UNIDAD["unidad_1"])


# ── Endpoints ───────────────────────────────────────────────────

@router.post("/iniciar")
def iniciar_diagnostico(
    req: IniciarRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if req.unidad_id not in UNIDADES_VALIDAS:
        raise HTTPException(status_code=400, detail=f"Unidad no válida: {req.unidad_id}")

    existente = db.query(DiagnosticoSesion).filter(
        DiagnosticoSesion.student_id == current_user.id,
        DiagnosticoSesion.unidad_id == req.unidad_id,
        DiagnosticoSesion.estado == "en_progreso",
    ).first()
    if existente:
        raise HTTPException(
            status_code=400,
            detail="Ya tienes una sesión en progreso para esta unidad",
        )

    completada = db.query(DiagnosticoSesion).filter(
        DiagnosticoSesion.student_id == current_user.id,
        DiagnosticoSesion.unidad_id == req.unidad_id,
        DiagnosticoSesion.estado == "completado",
    ).first()
    if completada:
        raise HTTPException(
            status_code=400,
            detail="Ya completaste la evaluación diagnóstica de esta unidad",
        )

    kc_orden = _kc_orden(req.unidad_id)
    banco = BANCO_POR_UNIDAD[req.unidad_id]

    sesion_id = str(uuid.uuid4())
    sesion = DiagnosticoSesion(
        id=sesion_id,
        student_id=current_user.id,
        unidad_id=req.unidad_id,
        estado="en_progreso",
        respuestas_json=[],
    )
    db.add(sesion)
    db.flush()

    for kc in kc_orden:
        params = BKT_PARAMS[kc]
        estado = BKTEstado(
            sesion_id=sesion_id,
            kc_dominio=kc,
            p_dominio=params.p_l0,
            nivel_actual=Nivel.BASICO,
            preguntas_respondidas=0,
            confirmadas_correctas=0,
            completado=False,
            nivel_confirmado=None,
        )
        db.add(estado)

    db.commit()

    primera = seleccionar_pregunta(banco, kc_orden[0], "BASICO", set())
    if not primera:
        raise HTTPException(status_code=500, detail="No hay preguntas disponibles")

    return {
        "sesion_id": sesion_id,
        "pregunta": _pregunta_publica(primera),
        "progreso": {
            "kc_actual": kc_orden[0],
            "kcs_completados": 0,
            "kcs_totales": len(kc_orden),
            "pregunta_num": 1,
        },
    }


@router.post("/responder")
def responder_pregunta(
    req: ResponderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sesion = db.query(DiagnosticoSesion).filter(
        DiagnosticoSesion.id == req.sesion_id,
        DiagnosticoSesion.student_id == current_user.id,
        DiagnosticoSesion.estado == "en_progreso",
    ).first()
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o ya completada")

    kc_orden = _kc_orden(sesion.unidad_id)
    banco = BANCO_POR_UNIDAD[sesion.unidad_id]

    pregunta = next((p for p in banco if p["pregunta_id"] == req.pregunta_id), None)
    if not pregunta:
        raise HTTPException(status_code=404, detail="Pregunta no encontrada")

    estados = _estados_ordenados(db, req.sesion_id, kc_orden)
    estado_actual = _siguiente_estado(estados)
    if estado_actual is None:
        raise HTTPException(status_code=400, detail="Todas las habilidades ya fueron evaluadas")

    _, letter_map = _shuffle_opciones(pregunta)
    respuesta_original = letter_map.get(req.respuesta.upper(), req.respuesta.upper())
    respuesta_correcta = pregunta["respuesta_correcta"]
    # Letra barajada que corresponde a la respuesta correcta (para feedback en frontend)
    respuesta_correcta_shuffled = next(s for s, o in letter_map.items() if o == respuesta_correcta)
    correcto = respuesta_correcta == respuesta_original
    params = BKT_PARAMS[estado_actual.kc_dominio]
    p_antes = estado_actual.p_dominio
    nueva_p = actualizar_p_dominio(p_antes, correcto, params)

    estado_actual.p_dominio = nueva_p
    estado_actual.preguntas_respondidas += 1
    if correcto:
        estado_actual.confirmadas_correctas += 1

    # Guardar en historial
    respuestas = list(sesion.respuestas_json or [])
    respuestas.append({
        "pregunta_id": req.pregunta_id,
        "kc_dominio": estado_actual.kc_dominio,
        "nivel": _nivel_str(estado_actual.nivel_actual),
        "respuesta_dada": respuesta_original,
        "respuesta_correcta": respuesta_correcta,
        "correcto": correcto,
        "p_dominio_antes": round(p_antes, 4),
        "p_dominio_despues": round(nueva_p, 4),
        "timestamp": datetime.utcnow().isoformat(),
    })
    sesion.respuestas_json = respuestas

    # Decidir progresión de KC
    nivel_idx = NIVEL_ORDEN.index(_nivel_str(estado_actual.nivel_actual))

    if nueva_p >= DOMINIO_UMBRAL:
        if nivel_idx == 2:  # ALTO dominado
            estado_actual.completado = True
            estado_actual.nivel_confirmado = Nivel.ALTO
        else:
            estado_actual.nivel_actual = Nivel(NIVEL_ORDEN[nivel_idx + 1])
            estado_actual.p_dominio = params.p_l0
    elif nueva_p < DESCENSO_UMBRAL and estado_actual.preguntas_respondidas >= MIN_PREGUNTAS_POR_KC:
        estado_actual.completado = True
        confirmado = NIVEL_ORDEN[max(0, nivel_idx - 1)]
        estado_actual.nivel_confirmado = Nivel(confirmado)
    elif estado_actual.preguntas_respondidas >= MAX_PREGUNTAS_POR_KC:
        estado_actual.completado = True
        estado_actual.nivel_confirmado = Nivel(_nivel_str(estado_actual.nivel_actual))

    db.commit()

    # Releer estados frescos para decidir siguiente acción
    estados_frescos = _estados_ordenados(db, req.sesion_id, kc_orden)
    siguiente_estado = _siguiente_estado(estados_frescos)
    respondidas_ids = {r["pregunta_id"] for r in respuestas}

    if siguiente_estado is None:
        # Sesión completada
        niveles = {
            e.kc_dominio: _nivel_str(e.nivel_confirmado or e.nivel_actual)
            for e in estados_frescos
        }
        nivel_g = nivel_promedio(niveles)

        sesion.estado = "completado"
        sesion.fecha_fin = datetime.utcnow()
        sesion.nivel_resultado_global = Nivel(nivel_g)
        current_user.nivel_actual = Nivel(nivel_g)
        db.commit()

        # Nivelar unidad anterior automáticamente si aplica
        unidad_num = int(sesion.unidad_id.split("_")[1])
        if unidad_num > 1:
            try:
                nivelar_unidad(f"unidad_{unidad_num - 1}", nivel_g, current_user.id, db)
            except Exception as e:
                logging.warning(f"nivelar_unidad falló (no crítico): {e}")

        return {
            "correcto": correcto,
            "respuesta_correcta": respuesta_correcta_shuffled,
            "explicacion": pregunta.get("explicacion", ""),
            "p_dominio_actual": round(nueva_p, 4),
            "siguiente": {
                "tipo": "resultado",
                "sesion_id": req.sesion_id,
                "pregunta": None,
                "progreso": None,
            },
        }

    sig_pregunta = seleccionar_pregunta(
        banco,
        siguiente_estado.kc_dominio,
        _nivel_str(siguiente_estado.nivel_actual),
        respondidas_ids,
    )

    # Safeguard: si no hay preguntas disponibles, completar el KC en su nivel actual
    if sig_pregunta is None:
        siguiente_estado.completado = True
        siguiente_estado.nivel_confirmado = Nivel(_nivel_str(siguiente_estado.nivel_actual))
        db.commit()
        estados_frescos = _estados_ordenados(db, req.sesion_id, kc_orden)
        if _siguiente_estado(estados_frescos) is None:
            niveles = {
                e.kc_dominio: _nivel_str(e.nivel_confirmado or e.nivel_actual)
                for e in estados_frescos
            }
            nivel_g = nivel_promedio(niveles)
            sesion.estado = "completado"
            sesion.fecha_fin = datetime.utcnow()
            sesion.nivel_resultado_global = Nivel(nivel_g)
            current_user.nivel_actual = Nivel(nivel_g)
            db.commit()

            unidad_num = int(sesion.unidad_id.split("_")[1])
            if unidad_num > 1:
                try:
                    nivelar_unidad(f"unidad_{unidad_num - 1}", nivel_g, current_user.id, db)
                except Exception as e:
                    logging.warning(f"nivelar_unidad falló (no crítico): {e}")

            return {
                "correcto": correcto,
                "respuesta_correcta": respuesta_correcta_shuffled,
                "explicacion": pregunta.get("explicacion", ""),
                "p_dominio_actual": round(nueva_p, 4),
                "siguiente": {
                    "tipo": "resultado",
                    "sesion_id": req.sesion_id,
                    "pregunta": None,
                    "progreso": None,
                },
            }
        siguiente_estado = _siguiente_estado(estados_frescos)
        sig_pregunta = seleccionar_pregunta(
            banco,
            siguiente_estado.kc_dominio,
            _nivel_str(siguiente_estado.nivel_actual),
            respondidas_ids,
        )

    kcs_completados = sum(1 for e in estados_frescos if e.completado)

    return {
        "correcto": correcto,
        "respuesta_correcta": respuesta_correcta_shuffled,
        "explicacion": pregunta.get("explicacion", ""),
        "p_dominio_actual": round(nueva_p, 4),
        "siguiente": {
            "tipo": "pregunta",
            "pregunta": _pregunta_publica(sig_pregunta) if sig_pregunta else None,
            "progreso": {
                "kc_actual": siguiente_estado.kc_dominio,
                "kcs_completados": kcs_completados,
                "kcs_totales": len(kc_orden),
                "pregunta_num": len(respuestas) + 1,
            },
        },
    }


def _resultado_de_sesion(sesion: DiagnosticoSesion, db: Session) -> dict:
    kc_orden = _kc_orden(sesion.unidad_id)
    estados = _estados_ordenados(db, sesion.id, kc_orden)

    detalle = []
    for e in estados:
        nivel_conf = _nivel_str(e.nivel_confirmado or e.nivel_actual)
        detalle.append({
            "kc": e.kc_dominio,
            "nivel_confirmado": nivel_conf,
            "p_dominio_final": round(e.p_dominio, 4),
            "preguntas_respondidas": e.preguntas_respondidas,
            "correctas": e.confirmadas_correctas,
        })

    nivel_g = _nivel_str(sesion.nivel_resultado_global or Nivel.BASICO)

    niveles_map = {d["kc"]: d["nivel_confirmado"] for d in detalle}
    kc_debil = min(
        kc_orden,
        key=lambda kc: NIVEL_ORDEN.index(niveles_map.get(kc, "BASICO")),
    )

    tiempo_min = None
    if sesion.fecha_fin and sesion.fecha_inicio:
        delta = sesion.fecha_fin - sesion.fecha_inicio
        tiempo_min = round(delta.total_seconds() / 60, 1)

    mensajes = {
        "ALTO": "Excelente dominio. Puedes abordar contenido avanzado de esta unidad.",
        "MEDIO": "Buen nivel. Puedes continuar con contenido de nivel medio.",
        "BASICO": "Nivel inicial identificado. Se recomienda reforzar los fundamentos.",
    }

    return {
        "nivel_global": nivel_g,
        "detalle_por_kc": detalle,
        "total_preguntas": len(sesion.respuestas_json or []),
        "tiempo_minutos": tiempo_min,
        "mensaje": mensajes.get(nivel_g, ""),
        "kc_mas_debil": kc_debil,
        "sesion_completada": sesion.estado == "completado",
    }


@router.get("/resultado/{sesion_id}")
def obtener_resultado(
    sesion_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sesion = db.query(DiagnosticoSesion).filter(
        DiagnosticoSesion.id == sesion_id,
        DiagnosticoSesion.student_id == current_user.id,
    ).first()
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    return _resultado_de_sesion(sesion, db)


@router.get("/resultado-unidad/{unidad_id}")
def obtener_resultado_unidad(
    unidad_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resultado de la última evaluación completada por el estudiante en esta unidad."""
    sesion = db.query(DiagnosticoSesion).filter(
        DiagnosticoSesion.student_id == current_user.id,
        DiagnosticoSesion.unidad_id == unidad_id,
        DiagnosticoSesion.estado == "completado",
    ).order_by(DiagnosticoSesion.fecha_fin.desc()).first()
    if not sesion:
        raise HTTPException(status_code=404, detail="No hay evaluación completada para esta unidad")

    return _resultado_de_sesion(sesion, db)


@router.get("/sesion-activa")
def sesion_activa(
    unidad_id: str = "unidad_1",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sesion = db.query(DiagnosticoSesion).filter(
        DiagnosticoSesion.student_id == current_user.id,
        DiagnosticoSesion.unidad_id == unidad_id,
        DiagnosticoSesion.estado == "en_progreso",
    ).first()
    if not sesion:
        return {"activa": False, "sesion": None}

    kc_orden = _kc_orden(unidad_id)
    banco = BANCO_POR_UNIDAD[unidad_id]
    estados = _estados_ordenados(db, sesion.id, kc_orden)
    siguiente = _siguiente_estado(estados)
    respondidas_ids = {r["pregunta_id"] for r in (sesion.respuestas_json or [])}

    sig_pregunta = None
    progreso = None
    if siguiente:
        sig_pregunta = seleccionar_pregunta(
            banco,
            siguiente.kc_dominio,
            _nivel_str(siguiente.nivel_actual),
            respondidas_ids,
        )
        kcs_completados = sum(1 for e in estados if e.completado)
        progreso = {
            "kc_actual": siguiente.kc_dominio,
            "kcs_completados": kcs_completados,
            "kcs_totales": len(kc_orden),
            "pregunta_num": len(sesion.respuestas_json or []) + 1,
        }

    return {
        "activa": True,
        "sesion": {
            "sesion_id": sesion.id,
            "pregunta": _pregunta_publica(sig_pregunta) if sig_pregunta else None,
            "progreso": progreso,
        },
    }
