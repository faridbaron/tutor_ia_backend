"""
Tests unitarios para el motor BKT (Bayesian Knowledge Tracing).
Ejecutar: python -m pytest tests/test_bkt.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bkt.engine import (
    actualizar_p_dominio,
    seleccionar_pregunta,
    nivel_global,
    BKT_PARAMS,
    DOMINIO_UMBRAL,
    DESCENSO_UMBRAL,
    KC_ORDEN,
    NIVEL_ORDEN,
)


# ── actualizar_p_dominio ─────────────────────────────────────────

def test_respuesta_correcta_aumenta_p():
    params = BKT_PARAMS["KC_ALGORITHMIC_THINKING"]
    p_inicial = 0.2
    p_nueva = actualizar_p_dominio(p_inicial, correcto=True, params=params)
    assert p_nueva > p_inicial, "Una respuesta correcta debe aumentar p_dominio"


def test_respuesta_incorrecta_disminuye_p():
    params = BKT_PARAMS["KC_ALGORITHMIC_THINKING"]
    p_inicial = 0.5
    p_nueva = actualizar_p_dominio(p_inicial, correcto=False, params=params)
    assert p_nueva < p_inicial, "Una respuesta incorrecta debe disminuir p_dominio"


def test_p_dominio_acotado():
    params = BKT_PARAMS["KC_ALGORITHMIC_THINKING"]
    p = 0.2
    for _ in range(20):
        p = actualizar_p_dominio(p, correcto=True, params=params)
    assert 0.0 <= p <= 1.0, "p_dominio debe permanecer entre 0 y 1"


def test_todas_correctas_supera_umbral():
    """Un estudiante que responde todo correcto debe superar el umbral de dominio."""
    params = BKT_PARAMS["KC_ALGORITHMIC_THINKING"]
    p = params.p_l0
    for _ in range(10):
        p = actualizar_p_dominio(p, correcto=True, params=params)
    assert p >= DOMINIO_UMBRAL, f"p={p:.4f} debe superar el umbral {DOMINIO_UMBRAL}"


def test_todas_incorrectas_cae_bajo_umbral():
    """Un estudiante que responde todo incorrecto debe caer bajo el umbral de descenso."""
    params = BKT_PARAMS["KC_ALGORITHMIC_THINKING"]
    p = params.p_l0
    for _ in range(5):
        p = actualizar_p_dominio(p, correcto=False, params=params)
    assert p < DESCENSO_UMBRAL, f"p={p:.4f} debe caer bajo el umbral de descenso {DESCENSO_UMBRAL}"


def test_cambio_nivel_al_superar_umbral():
    """Después de muchas respuestas correctas p supera 0.75 → debe cambiar de nivel."""
    params = BKT_PARAMS["KC_PATTERN_RECOGNITION"]
    p = params.p_l0
    superado = False
    for _ in range(15):
        p = actualizar_p_dominio(p, correcto=True, params=params)
        if p >= DOMINIO_UMBRAL:
            superado = True
            break
    assert superado, "Debe superar el umbral después de varias respuestas correctas"


# ── seleccionar_pregunta ─────────────────────────────────────────

def _banco_minimo():
    return [
        {"pregunta_id": "q1", "dominio": "KC_ABSTRACTION", "nivel": "BASICO", "enunciado": "P1"},
        {"pregunta_id": "q2", "dominio": "KC_ABSTRACTION", "nivel": "BASICO", "enunciado": "P2"},
        {"pregunta_id": "q3", "dominio": "KC_ABSTRACTION", "nivel": "MEDIO",  "enunciado": "P3"},
    ]


def test_seleccionar_excluye_respondidas():
    banco = _banco_minimo()
    p = seleccionar_pregunta(banco, "KC_ABSTRACTION", "BASICO", excluir_ids={"q1"})
    assert p is not None
    assert p["pregunta_id"] == "q2"


def test_seleccionar_sin_candidatas_devuelve_none():
    banco = _banco_minimo()
    p = seleccionar_pregunta(banco, "KC_ABSTRACTION", "BASICO", excluir_ids={"q1", "q2"})
    assert p is None


def test_seleccionar_filtra_por_kc_y_nivel():
    banco = _banco_minimo()
    p = seleccionar_pregunta(banco, "KC_ABSTRACTION", "MEDIO", excluir_ids=set())
    assert p is not None
    assert p["pregunta_id"] == "q3"


# ── nivel_global ─────────────────────────────────────────────────

def test_nivel_global_minimo():
    niveles = {
        "KC_ALGORITHMIC_THINKING": "ALTO",
        "KC_ABSTRACTION": "MEDIO",
        "KC_PATTERN_RECOGNITION": "BASICO",
        "KC_PROBLEM_DECOMPOSITION": "MEDIO",
    }
    assert nivel_global(niveles) == "BASICO"


def test_nivel_global_todos_alto():
    niveles = {kc: "ALTO" for kc in KC_ORDEN}
    assert nivel_global(niveles) == "ALTO"


def test_nivel_global_todos_basico():
    niveles = {kc: "BASICO" for kc in KC_ORDEN}
    assert nivel_global(niveles) == "BASICO"


def test_nivel_global_mixto_medio():
    niveles = {
        "KC_ALGORITHMIC_THINKING": "ALTO",
        "KC_ABSTRACTION": "ALTO",
        "KC_PATTERN_RECOGNITION": "MEDIO",
        "KC_PROBLEM_DECOMPOSITION": "ALTO",
    }
    assert nivel_global(niveles) == "MEDIO"


# ── Parámetros BKT ──────────────────────────────────────────────

def test_todos_los_kcs_tienen_parametros():
    for kc in KC_ORDEN:
        assert kc in BKT_PARAMS, f"Faltan parámetros para {kc}"


def test_parametros_validos():
    for kc, p in BKT_PARAMS.items():
        assert 0 < p.p_l0 < 1, f"p_l0 fuera de rango para {kc}"
        assert 0 < p.t < 1, f"t fuera de rango para {kc}"
        assert 0 < p.g < 1, f"g fuera de rango para {kc}"
        assert 0 < p.s < 1, f"s fuera de rango para {kc}"
