"""
Carga el grafo de prerrequisitos DIRECTAMENTE desde el Excel a Neo4j.
No necesita LLM — el Excel ya tiene los node_id, temas y relaciones listos.

Hoja "Temas (3 niveles) y Prereq." → nodos Tema
Hoja "Mapa Prerequisitos (Neo4j)"  → relaciones REQUIERE_PREVIO
"""

import logging
import openpyxl
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# Mapeo de nivel texto → entero (para dificultad)
NIVEL_A_INT = {"Básico": 1, "Medio": 2, "Alto": 3}

# Mapeo de dominio KC → descripción corta
KC_DISPLAY = {
    "KC_ALGORITHMIC_THINKING":  "Pensamiento Algorítmico",
    "KC_ABSTRACTION":           "Abstracción",
    "KC_PATTERN_RECOGNITION":   "Reconocimiento de Patrones",
    "KC_PROBLEM_DECOMPOSITION": "Descomposición de Problemas",
    "KC_PROPOSITIONAL_LOGIC":   "Lógica Proposicional y Condicional",
    "KC_LOOPS_AND_ITERATION":   "Bucles y Ciclos",
}


class ExcelToNeo4j:
    """
    Lee el Excel BaseConocimiento_Prerequisitos_v2.xlsx y carga todo en Neo4j.
    
    Modelo de grafo resultante:
    (:Nodo {node_id, nombre_display, tema_canonico, nivel, dificultad, 
            unidad, semanas, dominio, kc, bloom})
    -[:REQUIERE_PREVIO]->(:Nodo)
    """

    def __init__(self, neo4j_uri: str, neo4j_user: str, neo4j_pass: str):
        self.driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))

    def close(self):
        self.driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ──────────────────────────────────────────────────────────
    # CARGA PRINCIPAL
    # ──────────────────────────────────────────────────────────

    def cargar_desde_excel(self, ruta_excel: str):
        """
        Carga nodos y relaciones desde el Excel completo.
        Ejecutar con --limpiar antes si quieres empezar desde cero.
        """
        wb = openpyxl.load_workbook(ruta_excel, read_only=True)

        # 1. Crear índices
        self._crear_indices()

        # 2. Leer nodos de la hoja de temas
        nodos = self._leer_nodos(wb["Temas (3 niveles) y Prereq."])
        logger.info(f"Nodos leídos del Excel: {len(nodos)}")

        # 3. Leer relaciones de la hoja de mapa
        relaciones = self._leer_relaciones(wb["Mapa Prerequisitos (Neo4j)"])
        logger.info(f"Relaciones leídas del Excel: {len(relaciones)}")

        # 4. Insertar nodos
        self._insertar_nodos(nodos)

        # 5. Insertar relaciones
        self._insertar_relaciones(relaciones)

        logger.info(f"✓ Grafo cargado: {len(nodos)} nodos, {len(relaciones)} relaciones")
        return len(nodos), len(relaciones)

    # ──────────────────────────────────────────────────────────
    # LECTURA DEL EXCEL
    # ──────────────────────────────────────────────────────────

    def _leer_nodos(self, ws) -> list[dict]:
        """
        Lee la hoja 'Temas (3 niveles) y Prereq.' y construye la lista de nodos.
        Columnas: Unidad | Semanas | Dominio | Tema | Nivel | Bloom | Nivel curso | KC | Prerequisitos
        """
        nodos = []
        encabezado_encontrado = False

        for row in ws.iter_rows(values_only=True):
            # Detectar fila de encabezado
            if not encabezado_encontrado:
                if row[0] == "Unidad":
                    encabezado_encontrado = True
                continue

            # Ignorar filas vacías o de sección
            if not row[0] or not row[3] or not row[4]:
                continue
            if str(row[0]).startswith("UNIDAD"):
                continue

            unidad   = str(row[0]).strip()
            semanas  = str(row[1]).strip() if row[1] else ""
            dominio  = str(row[2]).strip() if row[2] else ""
            tema     = str(row[3]).strip()
            nivel    = str(row[4]).strip()   # Básico / Medio / Alto
            bloom    = str(row[5]).strip() if row[5] else ""
            kc       = str(row[7]).strip() if row[7] else ""

            # Construir node_id igual que en el Excel de mapa
            node_id = self._tema_a_node_id(tema, nivel)
            tema_canonico = self._tema_a_canonico(tema)

            nodos.append({
                "node_id":       node_id,
                "nombre_display": f"{tema} ({nivel})",
                "tema":          tema,
                "tema_canonico": tema_canonico,
                "nivel":         nivel.upper(),          # BASICO / MEDIO / ALTO
                "dificultad":    NIVEL_A_INT.get(nivel, 1),
                "unidad":        unidad,
                "semanas":       semanas,
                "dominio":       dominio,
                "kc":            kc,
                "bloom":         bloom,
                "chunks_count":  0,
            })

        return nodos

    def _leer_relaciones(self, ws) -> list[dict]:
        """
        Lee la hoja 'Mapa Prerequisitos (Neo4j)' y construye lista de relaciones.
        Columnas: Tema | Nivel | node_id | Prerequisito | Prerequisito node_id
        """
        relaciones = []
        encabezado_encontrado = False

        for row in ws.iter_rows(values_only=True):
            if not encabezado_encontrado:
                if row[0] == "Tema (nodo)":
                    encabezado_encontrado = True
                continue

            if not row[2] or not row[4]:
                continue
            if str(row[4]).strip() == "—":
                continue  # punto de partida, sin prerequisito

            hacia = str(row[2]).strip()    # node_id del tema actual
            desde = str(row[4]).strip()    # node_id del prerequisito

            if desde and hacia and desde != "—":
                relaciones.append({"desde": desde, "hacia": hacia})

        return relaciones

    # ──────────────────────────────────────────────────────────
    # INSERCIÓN EN NEO4J
    # ──────────────────────────────────────────────────────────

    def _crear_indices(self):
        queries = [
            "CREATE CONSTRAINT nodo_unique IF NOT EXISTS FOR (n:Nodo) REQUIRE n.node_id IS UNIQUE",
            "CREATE INDEX nodo_tema IF NOT EXISTS FOR (n:Nodo) ON (n.tema_canonico)",
            "CREATE INDEX nodo_nivel IF NOT EXISTS FOR (n:Nodo) ON (n.nivel)",
            "CREATE CONSTRAINT chunk_unique IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
        ]
        with self.driver.session() as session:
            for q in queries:
                try:
                    session.run(q)
                except Exception as e:
                    logger.debug(f"Índice ya existe: {e}")
        logger.info("Índices Neo4j verificados.")

    def _insertar_nodos(self, nodos: list[dict]):
        with self.driver.session() as session:
            for nodo in nodos:
                session.run("""
                    MERGE (n:Nodo {node_id: $node_id})
                    SET n.nombre_display = $nombre_display,
                        n.tema           = $tema,
                        n.tema_canonico  = $tema_canonico,
                        n.nivel          = $nivel,
                        n.dificultad     = $dificultad,
                        n.unidad         = $unidad,
                        n.semanas        = $semanas,
                        n.dominio        = $dominio,
                        n.kc             = $kc,
                        n.bloom          = $bloom,
                        n.chunks_count   = $chunks_count
                """, nodo)
        logger.info(f"  {len(nodos)} nodos insertados en Neo4j.")

    def _insertar_relaciones(self, relaciones: list[dict]):
        ok = 0
        skip = 0
        with self.driver.session() as session:
            for rel in relaciones:
                result = session.run("""
                    MATCH (desde:Nodo {node_id: $desde})
                    MATCH (hacia:Nodo {node_id: $hacia})
                    MERGE (desde)-[:REQUIERE_PREVIO]->(hacia)
                    RETURN desde.node_id AS d
                """, rel)
                if result.single():
                    ok += 1
                else:
                    skip += 1
                    logger.warning(f"  Relación no resuelta: {rel['desde']} → {rel['hacia']}")
        logger.info(f"  {ok} relaciones insertadas, {skip} no resueltas.")

    # ──────────────────────────────────────────────────────────
    # VINCULAR CHUNKS AL GRAFO (runtime del pipeline)
    # ──────────────────────────────────────────────────────────

    def insertar_chunk(self, chunk_data: dict, chunk_id: str):
        """
        Crea un nodo Chunk y lo vincula al Nodo (tema + nivel) correspondiente.
        chunk_data debe tener: tema_canonico, nivel_tema, tipo, fuente_libro, pagina, etc.
        """
        tema_canonico = chunk_data.get("tema_canonico", "")
        nivel_tema    = chunk_data.get("nivel_tema", "BASICO")
        node_id_tema  = f"{tema_canonico}_{nivel_tema}"

        with self.driver.session() as session:
            session.run("""
                MERGE (c:Chunk {chunk_id: $chunk_id})
                SET c.pinecone_id   = $chunk_id,
                    c.tipo          = $tipo,
                    c.tema_canonico = $tema_canonico,
                    c.nivel_tema    = $nivel_tema,
                    c.fuente_libro  = $fuente_libro,
                    c.pagina        = $pagina,
                    c.dificultad    = $dificultad,
                    c.conceptos     = $conceptos
            """, {
                "chunk_id":      chunk_id,
                "tipo":          chunk_data.get("tipo", ""),
                "tema_canonico": tema_canonico,
                "nivel_tema":    nivel_tema,
                "fuente_libro":  chunk_data.get("fuente_libro", ""),
                "pagina":        chunk_data.get("pagina", 0),
                "dificultad":    chunk_data.get("dificultad", 1),
                "conceptos":     chunk_data.get("conceptos", []),
            })

            # Vincular al nodo de tema
            session.run("""
                MATCH (n:Nodo {node_id: $node_id})
                MATCH (c:Chunk {chunk_id: $chunk_id})
                MERGE (n)-[:TIENE_CONTENIDO]->(c)
                WITH n SET n.chunks_count = n.chunks_count + 1
            """, {"node_id": node_id_tema, "chunk_id": chunk_id})

    # ──────────────────────────────────────────────────────────
    # CONSULTAS PARA EL TUTOR (runtime)
    # ──────────────────────────────────────────────────────────

    def prerequisitos_faltantes(self, node_id: str,
                                 temas_dominados: list[str]) -> list[dict]:
        """
        Dado el node_id del tema que quiere ver el estudiante,
        retorna los prerequisitos que aún no domina.
        temas_dominados: lista de node_id ya superados por el estudiante.
        """
        with self.driver.session() as session:
            result = session.run("""
                MATCH (pre:Nodo)-[:REQUIERE_PREVIO]->(t:Nodo {node_id: $node_id})
                WHERE NOT pre.node_id IN $dominados
                RETURN pre.node_id AS node_id, pre.nombre_display AS nombre,
                       pre.dificultad AS dificultad
                ORDER BY pre.dificultad ASC
            """, {"node_id": node_id, "dominados": temas_dominados})
            return [dict(r) for r in result]

    def chunks_de_nodo(self, node_id: str, tipo: str = None) -> list[dict]:
        """Retorna los chunks de un nodo específico (tema + nivel)."""
        filtro = "WHERE n.node_id = $node_id"
        params = {"node_id": node_id}
        if tipo:
            filtro += " AND c.tipo = $tipo"
            params["tipo"] = tipo

        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (n:Nodo)-[:TIENE_CONTENIDO]->(c:Chunk)
                {filtro}
                RETURN c.chunk_id AS chunk_id, c.tipo AS tipo,
                       c.fuente_libro AS fuente, c.pagina AS pagina
                ORDER BY c.tipo ASC
            """, params)
            return [dict(r) for r in result]

    def ruta_aprendizaje(self, node_id_destino: str) -> list[str]:
        """Orden topológico de nodos para llegar al destino."""
        with self.driver.session() as session:
            result = session.run("""
                MATCH path = (inicio:Nodo)-[:REQUIERE_PREVIO*]->(dest:Nodo {node_id: $dest})
                WHERE NOT ()-[:REQUIERE_PREVIO]->(inicio)
                RETURN [n IN nodes(path) | n.node_id] AS ruta
                ORDER BY length(path) DESC LIMIT 1
            """, {"dest": node_id_destino})
            record = result.single()
            return record["ruta"] if record else [node_id_destino]

    # ──────────────────────────────────────────────────────────
    # UTILIDADES
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _tema_a_canonico(tema: str) -> str:
        """
        Convierte el nombre de tema del Excel al tema_canonico snake_case.
        Usa la misma lógica que el Excel para generar el node_id base.
        """
        import re
        # Normalizar: minúsculas, quitar acentos básicos, reemplazar espacios y guiones
        t = tema.lower()
        t = t.replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u")
        t = t.replace("ñ","n")
        t = re.sub(r"[^a-z0-9]+", "_", t)
        t = t.strip("_")
        return t

    @staticmethod
    def _tema_a_node_id(tema: str, nivel: str) -> str:
        """Genera el node_id completo: tema_canonico + '_' + NIVEL."""
        canonico = ExcelToNeo4j._tema_a_canonico(tema)
        nivel_upper = nivel.upper().replace("Á","A").replace("É","E").replace("Í","I")
        # Normalizar nivel a BASICO/MEDIO/ALTO
        if "ASICO" in nivel_upper or nivel_upper == "BASICO":
            sufijo = "BASICO"
        elif "EDIO" in nivel_upper or nivel_upper == "MEDIO":
            sufijo = "MEDIO"
        else:
            sufijo = "ALTO"
        return f"{canonico}_{sufijo}"


# ─────────────────────────────────────────────────────────────
# Script de carga standalone
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    ruta_excel = os.environ.get("EXCEL_PATH", "BaseConocimiento_Prerequisitos_v2.xlsx")
    neo4j_uri  = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USER",     "neo4j")
    neo4j_pass = os.environ.get("NEO4J_PASSWORD",  "password")

    with ExcelToNeo4j(neo4j_uri, neo4j_user, neo4j_pass) as loader:
        n_nodos, n_rels = loader.cargar_desde_excel(ruta_excel)
        print(f"\n✓ Grafo listo: {n_nodos} nodos · {n_rels} relaciones")
        print("  Abre http://localhost:7474 para visualizarlo")
        print("  Query de prueba:")
        print("  MATCH (n:Nodo)-[:REQUIERE_PREVIO]->(m:Nodo)")
        print("  WHERE n.tema_canonico = 'ciclo_for_estructura_basica'")
        print("  RETURN n, m LIMIT 10")
