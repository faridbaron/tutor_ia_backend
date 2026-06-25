from dataclasses import dataclass
from typing import Optional
import random


@dataclass
class BKTParams:
    p_l0: float
    t: float
    g: float
    s: float


BKT_PARAMS: dict[str, BKTParams] = {
    "KC_ALGORITHMIC_THINKING":  BKTParams(p_l0=0.2,  t=0.35, g=0.15, s=0.1),
    "KC_ABSTRACTION":           BKTParams(p_l0=0.2,  t=0.30, g=0.15, s=0.1),
    "KC_PATTERN_RECOGNITION":   BKTParams(p_l0=0.25, t=0.30, g=0.2,  s=0.1),
    "KC_PROBLEM_DECOMPOSITION": BKTParams(p_l0=0.25, t=0.30, g=0.2,  s=0.1),
    "KC_PROPOSITIONAL_LOGIC":   BKTParams(p_l0=0.2,  t=0.35, g=0.15, s=0.1),
    "KC_LOOPS_AND_ITERATION":   BKTParams(p_l0=0.2,  t=0.30, g=0.15, s=0.1),
}

DOMINIO_UMBRAL = 0.75
DESCENSO_UMBRAL = 0.4
MAX_PREGUNTAS_POR_KC = 5
MIN_PREGUNTAS_POR_KC = 3  # Mínimo antes de cerrar un KC por descenso

KC_ORDEN_POR_UNIDAD: dict[str, list[str]] = {
    "unidad_1": [
        "KC_ALGORITHMIC_THINKING",
        "KC_ABSTRACTION",
        "KC_PATTERN_RECOGNITION",
        "KC_PROBLEM_DECOMPOSITION",
    ],
    "unidad_2": [
        "KC_PROPOSITIONAL_LOGIC",
        "KC_LOOPS_AND_ITERATION",
        "KC_PATTERN_RECOGNITION",
        "KC_PROBLEM_DECOMPOSITION",
    ],
    "unidad_3": [
        "KC_ABSTRACTION",
        "KC_LOOPS_AND_ITERATION",
        "KC_PATTERN_RECOGNITION",
        "KC_PROBLEM_DECOMPOSITION",
        "KC_ALGORITHMIC_THINKING",
    ],
}

# Alias unidad_1 para backward compat (tests)
KC_ORDEN = KC_ORDEN_POR_UNIDAD["unidad_1"]

NIVEL_ORDEN = ["BASICO", "MEDIO", "ALTO"]


def actualizar_p_dominio(p_ln: float, correcto: bool, params: BKTParams) -> float:
    if correcto:
        p_evidencia = p_ln * (1 - params.s) + (1 - p_ln) * params.g
        p_l_dado = (p_ln * (1 - params.s)) / p_evidencia
    else:
        p_evidencia = p_ln * params.s + (1 - p_ln) * (1 - params.g)
        p_l_dado = (p_ln * params.s) / p_evidencia
    return p_l_dado + (1 - p_l_dado) * params.t


def seleccionar_pregunta(
    banco: list, kc: str, nivel: str, excluir_ids: set
) -> Optional[dict]:
    candidatas = [
        p for p in banco
        if p["dominio"] == kc
        and p["nivel"] == nivel
        and p["pregunta_id"] not in excluir_ids
    ]
    return random.choice(candidatas) if candidatas else None


def nivel_global(niveles_por_kc: dict) -> str:
    minimo = len(NIVEL_ORDEN) - 1
    for nivel in niveles_por_kc.values():
        if nivel in NIVEL_ORDEN:
            idx = NIVEL_ORDEN.index(nivel)
            if idx < minimo:
                minimo = idx
    return NIVEL_ORDEN[minimo]
