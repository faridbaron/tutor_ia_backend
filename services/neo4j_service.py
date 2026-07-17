import os
from neo4j import GraphDatabase
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()

_driver = None


def _get_driver():
    global _driver
    if _driver is not None:
        return _driver
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USER")
    password = os.environ.get("NEO4J_PASSWORD")
    if not all([uri, user, password]):
        raise HTTPException(503, "Configuración de Neo4j incompleta (NEO4J_URI/USER/PASSWORD)")
    try:
        _driver = GraphDatabase.driver(uri, auth=(user, password))
        _driver.verify_connectivity()
    except Exception as e:
        _driver = None
        raise HTTPException(503, f"No se pudo conectar a Neo4j: {e}")
    return _driver


def get_nodos_unidad(unidad_num: int, max_dificultad: int) -> list[dict]:
    driver = _get_driver()
    with driver.session() as s:
        result = s.run(
            """
            MATCH (t:Tema)
            WHERE toLower(t.unidad) CONTAINS toLower($unidad) AND t.dificultad <= $max_dif
            RETURN t
            ORDER BY t.dificultad, t.tema_canonico
            """,
            unidad=f"unidad {unidad_num}",
            max_dif=max_dificultad,
        )
        return [dict(r["t"]) for r in result]


def get_nodos_unidad_nivel(unidad_num: int, dificultad: int) -> list[dict]:
    """Nodos de la unidad en el nivel EXACTO diagnosticado (no acumulativo).

    A diferencia de get_nodos_unidad (<=), usado para dar por dominada una unidad
    anterior completa, esta se usa para construir la ruta de la unidad actual: cada
    tema existe como un nodo distinto por nivel (mismo tema, node_id/tema_canonico
    distintos para BASICO/MEDIO/ALTO), así que traer <= mezclaría niveles inferiores
    del mismo tema como si fueran temas pendientes aparte.
    """
    driver = _get_driver()
    with driver.session() as s:
        result = s.run(
            """
            MATCH (t:Tema)
            WHERE toLower(t.unidad) CONTAINS toLower($unidad) AND t.dificultad = $dif
            RETURN t
            ORDER BY t.tema_canonico
            """,
            unidad=f"unidad {unidad_num}",
            dif=dificultad,
        )
        return [dict(r["t"]) for r in result]


def get_prereqs_nodos(node_ids: list[str]) -> dict[str, list[str]]:
    """Retorna {node_id: [prereq_tema_canonico]} para todos los nodos dados en una sola query."""
    if not node_ids:
        return {}
    driver = _get_driver()
    with driver.session() as s:
        result = s.run(
            """
            MATCH (pre:Tema)-[:REQUIERE_PREVIO]->(t:Tema)
            WHERE t.tema_canonico IN $ids
            RETURN t.tema_canonico AS node, pre.tema_canonico AS prereq
            """,
            ids=node_ids,
        )
        prereqs: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for r in result:
            prereqs[r["node"]].append(r["prereq"])
        return prereqs


def get_nodos_info(node_ids: list[str]) -> dict[str, dict]:
    """Retorna {tema_canonico: propiedades} para los nodos dados."""
    if not node_ids:
        return {}
    driver = _get_driver()
    with driver.session() as s:
        result = s.run(
            "MATCH (t:Tema) WHERE t.tema_canonico IN $ids RETURN t",
            ids=node_ids,
        )
        return {r["t"]["tema_canonico"]: dict(r["t"]) for r in result}


def get_chunk_ids_por_tipo(node_id: str) -> dict[str, list[str]]:
    """Retorna {tipo: [chunk_id]} para los chunks de un nodo en Neo4j."""
    driver = _get_driver()
    with driver.session() as s:
        result = s.run(
            """
            MATCH (t:Tema {tema_canonico: $node_id})-[:TIENE_CONTENIDO]->(c:Chunk)
            RETURN c.tipo AS tipo, c.chunk_id AS chunk_id
            """,
            node_id=node_id,
        )
        por_tipo: dict[str, list[str]] = {}
        for r in result:
            por_tipo.setdefault(r["tipo"], []).append(r["chunk_id"])
    return por_tipo
