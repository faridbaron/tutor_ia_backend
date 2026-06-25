"""
Constructor del grafo de prerrequisitos en Neo4j.
Crea nodos Tema y relaciones REQUIERE_PREVIO + TIENE_CONTENIDO.
"""

import logging
from neo4j import GraphDatabase
from schemas.chunk_schema import (
    ChunkTexto, ChunkEjercicioResuelto,
    ChunkImagenEnunciado, ChunkDiagrama,
)

logger = logging.getLogger(__name__)

# Tipo unión de todos los chunks posibles
ChunkCualquiera = ChunkTexto | ChunkEjercicioResuelto | ChunkImagenEnunciado | ChunkDiagrama


class Neo4jBuilder:
    """
    Gestiona la base de datos de grafos Neo4j para el tutor.

    Modelo del grafo:
    (:Tema)-[:REQUIERE_PREVIO]->(:Tema)
    (:Tema)-[:TIENE_CONTENIDO]->(:Chunk)
    (:Chunk)-[:DE_LIBRO]->(:Libro)
    """

    def __init__(self, uri: str, usuario: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(usuario, password))

    def close(self):
        self.driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ──────────────────────────────────────────────────────────
    # INICIALIZACIÓN
    # ──────────────────────────────────────────────────────────

    def crear_indices(self):
        """Crea índices para búsquedas rápidas. Ejecutar una sola vez."""
        queries = [
            "CREATE CONSTRAINT tema_unique IF NOT EXISTS FOR (t:Tema) REQUIRE t.tema_canonico IS UNIQUE",
            "CREATE CONSTRAINT chunk_unique IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
            "CREATE INDEX tema_nombre IF NOT EXISTS FOR (t:Tema) ON (t.nombre_display)",
        ]
        with self.driver.session() as session:
            for q in queries:
                try:
                    session.run(q)
                except Exception as e:
                    logger.warning(f"Índice ya existe o error: {e}")
        logger.info("Índices Neo4j creados.")

    def limpiar_todo(self):
        """CUIDADO: elimina todos los nodos y relaciones. Solo para desarrollo."""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.warning("Base de datos Neo4j limpiada completamente.")

    # ──────────────────────────────────────────────────────────
    # CARGAR GRAFO DE TEMAS (desde Excel / PROMPT_GRAFO_PREREQUISITOS)
    # ──────────────────────────────────────────────────────────

    def cargar_grafo_temas(self, grafo: dict):
        """
        Recibe el JSON del PROMPT_GRAFO_PREREQUISITOS y lo carga en Neo4j.
        grafo = {"nodos": [...], "relaciones": [...]}
        """
        with self.driver.session() as session:
            # Crear nodos Tema
            for nodo in grafo.get("nodos", []):
                session.run("""
                    MERGE (t:Tema {tema_canonico: $tema_canonico})
                    SET t.nombre_display      = $nombre_display,
                        t.descripcion         = $descripcion,
                        t.dificultad          = $dificultad,
                        t.tiempo_horas        = $tiempo_horas,
                        t.unidad              = $unidad,
                        t.chunks_count        = 0
                """, {
                    "tema_canonico": nodo["tema_canonico"],
                    "nombre_display": nodo.get("nombre_display", nodo["tema_canonico"]),
                    "descripcion":    nodo.get("descripcion", ""),
                    "dificultad":     nodo.get("dificultad", 1),
                    "tiempo_horas":   nodo.get("tiempo_estimado_horas", 1),
                    "unidad":         nodo.get("unidad", ""),
                })

            # Crear relaciones REQUIERE_PREVIO
            for rel in grafo.get("relaciones", []):
                session.run("""
                    MATCH (desde:Tema {tema_canonico: $desde})
                    MATCH (hacia:Tema {tema_canonico: $hacia})
                    MERGE (desde)-[:REQUIERE_PREVIO]->(hacia)
                """, {
                    "desde": rel["desde"],
                    "hacia": rel["hacia"],
                })

        logger.info(f"Grafo cargado: {len(grafo.get('nodos', []))} temas, "
                    f"{len(grafo.get('relaciones', []))} relaciones.")

    # ──────────────────────────────────────────────────────────
    # VINCULAR CHUNKS AL GRAFO
    # ──────────────────────────────────────────────────────────

    def insertar_chunk(self, chunk: ChunkCualquiera, chunk_id: str,
                        pinecone_id: str):
        """
        Crea un nodo Chunk en Neo4j y lo vincula al nodo Tema correspondiente.
        chunk_id:    ID único del chunk (ej: "libro1_p45_img0")
        pinecone_id: ID del vector en Pinecone (para recuperación semántica)
        """
        data = chunk.model_dump()

        with self.driver.session() as session:
            # Crear/actualizar nodo Chunk
            session.run("""
                MERGE (c:Chunk {chunk_id: $chunk_id})
                SET c.pinecone_id    = $pinecone_id,
                    c.tipo           = $tipo,
                    c.tema_canonico  = $tema_canonico,
                    c.fuente_libro   = $fuente_libro,
                    c.pagina         = $pagina,
                    c.dificultad     = $dificultad,
                    c.conceptos      = $conceptos
            """, {
                "chunk_id":      chunk_id,
                "pinecone_id":   pinecone_id,
                "tipo":          data.get("tipo"),
                "tema_canonico": data.get("tema_canonico"),
                "fuente_libro":  data.get("fuente_libro"),
                "pagina":        data.get("pagina"),
                "dificultad":    data.get("dificultad"),
                "conceptos":     data.get("conceptos", []),
            })

            # Vincular Tema → Chunk (TIENE_CONTENIDO)
            session.run("""
                MATCH (t:Tema {tema_canonico: $tema_canonico})
                MATCH (c:Chunk {chunk_id: $chunk_id})
                MERGE (t)-[:TIENE_CONTENIDO]->(c)
                WITH t
                SET t.chunks_count = t.chunks_count + 1
            """, {
                "tema_canonico": data.get("tema_canonico"),
                "chunk_id":      chunk_id,
            })

    # ──────────────────────────────────────────────────────────
    # CONSULTAS PARA EL TUTOR (runtime)
    # ──────────────────────────────────────────────────────────

    def prerequisitos_de(self, tema_canonico: str) -> list[dict]:
        """Retorna los temas que son prerequisito directo de un tema dado."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (pre:Tema)-[:REQUIERE_PREVIO]->(t:Tema {tema_canonico: $tema})
                RETURN pre.tema_canonico AS tema, pre.nombre_display AS nombre,
                       pre.dificultad AS dificultad
            """, {"tema": tema_canonico})
            return [dict(r) for r in result]

    def chunks_de_tema(self, tema_canonico: str,
                        tipo: str = None,
                        dificultad: int = None) -> list[dict]:
        """
        Retorna los chunks de un tema, con filtros opcionales de tipo y dificultad.
        Útil para que el tutor recupere el contenido más apropiado.
        """
        filtros = "WHERE c.tema_canonico = $tema"
        params  = {"tema": tema_canonico}

        if tipo:
            filtros += " AND c.tipo = $tipo"
            params["tipo"] = tipo
        if dificultad:
            filtros += " AND c.dificultad = $dificultad"
            params["dificultad"] = dificultad

        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (t:Tema)-[:TIENE_CONTENIDO]->(c:Chunk)
                {filtros}
                RETURN c.chunk_id AS chunk_id, c.pinecone_id AS pinecone_id,
                       c.tipo AS tipo, c.fuente_libro AS fuente,
                       c.pagina AS pagina, c.dificultad AS dificultad
                ORDER BY c.dificultad ASC, c.fuente_libro ASC
            """, params)
            return [dict(r) for r in result]

    def ruta_aprendizaje(self, tema_destino: str) -> list[str]:
        """
        Calcula el orden topológico de temas para llegar a un tema destino.
        Útil para que el tutor planifique la secuencia de enseñanza.
        """
        with self.driver.session() as session:
            result = session.run("""
                MATCH path = (inicio:Tema)-[:REQUIERE_PREVIO*]->(destino:Tema {tema_canonico: $tema})
                WHERE NOT ()-[:REQUIERE_PREVIO]->(inicio)
                RETURN [n IN nodes(path) | n.tema_canonico] AS ruta
                ORDER BY length(path) DESC
                LIMIT 1
            """, {"tema": tema_destino})
            record = result.single()
            if record:
                return record["ruta"]
            return [tema_destino]
