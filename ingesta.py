"""
Pipeline principal de ingesta.
Orquesta: PyMuPDF → GPT-4o Vision → Pydantic → Pinecone + Neo4j
"""

import os
import json
import logging
import argparse
import hashlib
from pathlib import Path

from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec

from extractors.pdf_extractor import PDFExtractor, agrupar_en_chunks
from extractors.llm_extractor import LLMExtractor
from pipeline.neo4j_builder import Neo4jBuilder

# ── Configuración de logging ──────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline")

# ── Constantes ────────────────────────────────────────────────
PINECONE_INDEX   = "tutor-logica-computacional"
EMBEDDING_MODEL  = "text-embedding-3-small"
EMBEDDING_DIM    = 1536
PINECONE_CLOUD   = "aws"
PINECONE_REGION  = "us-east-1"


# ─────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────

def chunk_id(fuente: str, pagina: int, idx: int, tipo: str) -> str:
    """Genera un ID único y legible para cada chunk."""
    return f"{fuente}_p{pagina}_{tipo}_{idx}"


def texto_para_embedding(chunk_data: dict) -> str:
    """
    Construye el texto que se va a embeber en Pinecone.
    Combina los campos más semánticamente ricos.
    """
    partes = []
    if tema := chunk_data.get("tema_canonico"):
        partes.append(f"Tema: {tema}")
    if tipo := chunk_data.get("tipo"):
        partes.append(f"Tipo: {tipo}")
    if conceptos := chunk_data.get("conceptos"):
        partes.append(f"Conceptos: {', '.join(conceptos)}")

    # Contenido principal según tipo
    if contenido := chunk_data.get("contenido"):
        partes.append(contenido)
    elif enunciado := chunk_data.get("enunciado_texto"):
        partes.append(enunciado)
    elif descripcion := chunk_data.get("descripcion"):
        partes.append(descripcion)
    elif codigo := chunk_data.get("codigo_pseint"):
        partes.append(f"Código PSeInt:\n{codigo}")
    elif desc_visual := chunk_data.get("descripcion_visual"):
        partes.append(desc_visual)

    return "\n".join(partes)[:8000]  # límite seguro para text-embedding-3-small


def embeber_texto(cliente_openai: OpenAI, texto: str) -> list[float]:
    """Genera el vector de embedding para un texto."""
    resp = cliente_openai.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texto,
    )
    return resp.data[0].embedding


# ─────────────────────────────────────────────────────────────
# SETUP PINECONE
# ─────────────────────────────────────────────────────────────

def setup_pinecone(api_key: str) -> any:
    """Crea el índice Pinecone si no existe y retorna el objeto index."""
    pc = Pinecone(api_key=api_key)

    indices_existentes = [i.name for i in pc.list_indexes()]
    if PINECONE_INDEX not in indices_existentes:
        logger.info(f"Creando índice Pinecone '{PINECONE_INDEX}'...")
        pc.create_index(
            name=PINECONE_INDEX,
            dimension=EMBEDDING_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud=PINECONE_CLOUD, region=PINECONE_REGION),
        )
        logger.info("Índice creado.")
    else:
        logger.info(f"Índice '{PINECONE_INDEX}' ya existe.")

    return pc.Index(PINECONE_INDEX)


# ─────────────────────────────────────────────────────────────
# GRAFO DE PRERREQUISITOS DESDE EXCEL
# ─────────────────────────────────────────────────────────────

def _generar_grafo_dict(ruta_excel: str) -> dict:
    """Lee el Excel y devuelve el grafo como dict {nodos, relaciones} sin tocar Neo4j."""
    import openpyxl
    import re as _re
    import unicodedata

    def _canonico(nombre: str) -> str:
        s = str(nombre or "").strip().lower()
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        s = _re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")

    NIVEL_SUFIJO = {"básico": "BASICO", "medio": "MEDIO", "alto": "ALTO"}
    NIVEL_NUM    = {"básico": 1, "medio": 2, "alto": 3}

    def _node_id(tema: str, nivel: str) -> str:
        return f"{_canonico(tema)}_{NIVEL_SUFIJO[nivel.lower()]}"

    def _split_prereqs(texto: str) -> list[str]:
        """Divide la lista de prerrequisitos respetando comas dentro de paréntesis."""
        partes, actual, depth = [], [], 0
        for ch in texto:
            if ch == "(":
                depth += 1
                actual.append(ch)
            elif ch == ")":
                depth -= 1
                actual.append(ch)
            elif ch == "," and depth == 0:
                p = "".join(actual).strip()
                if p:
                    partes.append(p)
                actual = []
            else:
                actual.append(ch)
        p = "".join(actual).strip()
        if p:
            partes.append(p)
        return partes

    def _parse_prereq(texto: str):
        import unicodedata as _ud
        texto = _ud.normalize("NFC", texto.strip())
        m = _re.match(r"^(.+?)\s*\((Básico|Medio|Alto)\)\s*$", texto)
        if not m:
            return None
        nivel = _ud.normalize("NFC", m.group(2).strip())
        if nivel.lower() not in NIVEL_SUFIJO:
            return None
        return _node_id(m.group(1).strip(), nivel)

    wb = openpyxl.load_workbook(ruta_excel)
    ws = wb["Temas"]

    nodos:     dict[str, dict] = {}
    relaciones: set[tuple[str, str]] = set()

    for fila in ws.iter_rows(min_row=2, values_only=True):
        cols = (fila + (None,) * 5)[:5]
        unidad_raw, tema_raw, nivel_raw, dominio_raw, prereqs_raw = cols

        if not tema_raw or not nivel_raw:
            continue
        import unicodedata as _ud
        nivel_str = _ud.normalize("NFC", str(nivel_raw).strip())
        if nivel_str.lower() not in NIVEL_SUFIJO:
            continue

        tema   = str(tema_raw).strip()
        nid    = _node_id(tema, nivel_str)
        m      = _re.search(r"unidad\s*(\d+)", str(unidad_raw or ""), _re.IGNORECASE)
        unidad = f"Unidad {m.group(1)}" if m else str(unidad_raw or "").strip()

        nodos[nid] = {
            "tema_canonico":         nid,
            "nombre_display":        f"{tema} ({nivel_str})",
            "descripcion":           "",
            "dificultad":            NIVEL_NUM[nivel_str.lower()],
            "tiempo_estimado_horas": 1,
            "unidad":                unidad,
        }

        for parte in _split_prereqs(str(prereqs_raw or "")):
            pre_nid = _parse_prereq(parte)
            if pre_nid and pre_nid != nid:
                relaciones.add((pre_nid, nid))

    relaciones = {(p, t) for p, t in relaciones if p in nodos and t in nodos}

    return {
        "nodos": list(nodos.values()),
        "relaciones": [{"desde": p, "hacia": t} for p, t in relaciones],
    }


def generar_grafo_desde_excel(ruta_excel: str, neo4j: Neo4jBuilder):
    """Lee el Excel y carga el grafo en Neo4j (primera ingesta, cuando Neo4j está vacío)."""
    grafo = _generar_grafo_dict(ruta_excel)

    logger.info(
        f"Grafo desde Excel: {len(grafo['nodos'])} nodos, "
        f"{len(grafo['relaciones'])} relaciones"
    )

    Path("output").mkdir(exist_ok=True)
    with open("output/grafo_prerequisitos.json", "w", encoding="utf-8") as f:
        json.dump(grafo, f, ensure_ascii=False, indent=2)
    logger.info("Grafo guardado en output/grafo_prerequisitos.json")

    neo4j.cargar_grafo_temas(grafo)


# ─────────────────────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────────

def procesar_libro(ruta_pdf: str, fuente_libro: str,
                   llm: LLMExtractor, openai_client: OpenAI,
                   pinecone_index, neo4j: Neo4jBuilder,
                   chunks_procesados: list):
    """
    Procesa un libro completo:
    1. Extrae texto e imágenes con PyMuPDF
    2. Estructura chunks de texto con GPT-4.1
    3. Procesa imágenes con GPT-4o Vision
    4. Valida con Pydantic
    5. Embebe y sube a Pinecone
    6. Inserta en Neo4j
    """
    logger.info(f"=== Procesando {fuente_libro}: {ruta_pdf} ===")
    vectors_batch = []  # batch para Pinecone (máx 100 por upsert)
    idx_global = 0

    with PDFExtractor(ruta_pdf, fuente_libro) as extractor:
        paginas = extractor.extraer_todo()
        logger.info(f"Páginas extraídas: {len(paginas)}")

        # ── Chunks de texto ───────────────────────────────────
        chunks_texto_raw = agrupar_en_chunks(paginas, max_chars=1500)
        logger.info(f"Chunks de texto a estructurar: {len(chunks_texto_raw)}")

        for raw in chunks_texto_raw:
            if len(raw["texto"].strip()) < 100:
                continue  # ignorar chunks muy pequeños

            chunk = llm.estructurar_chunk_texto(
                texto=raw["texto"],
                fuente_libro=fuente_libro,
                pagina=raw["pagina"],
                capitulo=raw.get("capitulo", ""),
                seccion=raw.get("seccion", ""),
            )
            if not chunk:
                continue

            cid = chunk_id(fuente_libro, raw["pagina"], idx_global, "texto")
            idx_global += 1

            # Embedding
            texto_embed = texto_para_embedding(chunk.model_dump())
            vector = embeber_texto(openai_client, texto_embed)

            # Metadata para Pinecone (sin el contenido completo, solo campos filtrables)
            metadata = {
                "chunk_id":      cid,
                "tipo":          chunk.tipo,
                "tema_canonico": chunk.tema_canonico,
                "fuente_libro":  fuente_libro,
                "pagina":        raw["pagina"],
                "dificultad":    int(chunk.dificultad),
                "conceptos":     chunk.conceptos,
                # Contenido completo para recuperación
                "contenido":     chunk.contenido[:1000],
            }
            vectors_batch.append((cid, vector, metadata))

            # Neo4j
            neo4j.insertar_chunk(chunk, cid, cid)
            chunks_procesados.append({"id": cid, "tipo": "texto", **chunk.model_dump()})

            # Flush batch cada 50 vectores
            if len(vectors_batch) >= 50:
                pinecone_index.upsert(vectors=vectors_batch)
                vectors_batch = []
                logger.info(f"  Batch subido a Pinecone. Total chunks: {idx_global}")

        # ── Imágenes ──────────────────────────────────────────
        enunciado_previo = ""  # buffer del enunciado de texto más cercano

        for pagina in paginas:
            # Actualizar el buffer de enunciado con el último texto de esta página
            if pagina.bloques_texto:
                enunciado_previo = " ".join(
                    b.texto for b in pagina.bloques_texto[-3:]
                )[:500]

            for imagen in pagina.imagenes:
                chunk = llm.procesar_imagen(imagen, fuente_libro, enunciado_previo)
                if not chunk:
                    continue

                tipo_str = chunk.tipo if isinstance(chunk.tipo, str) else chunk.tipo.value
                cid = chunk_id(fuente_libro, imagen.pagina, imagen.indice, tipo_str)

                texto_embed = texto_para_embedding(chunk.model_dump())
                vector = embeber_texto(openai_client, texto_embed)

                chunk_dict = chunk.model_dump()
                metadata = {
                    "chunk_id":      cid,
                    "tipo":          tipo_str,
                    "tema_canonico": chunk_dict.get("tema_canonico", ""),
                    "fuente_libro":  fuente_libro,
                    "pagina":        imagen.pagina,
                    "dificultad":    int(chunk_dict.get("dificultad", 1)),
                    "conceptos":     chunk_dict.get("conceptos", []),
                    "contenido":     (
                        chunk_dict.get("codigo_pseint", "")
                        or chunk_dict.get("enunciado_texto", "")
                        or chunk_dict.get("descripcion_visual", "")
                        or chunk_dict.get("descripcion", "")
                    )[:1000],
                }
                vectors_batch.append((cid, vector, metadata))

                neo4j.insertar_chunk(chunk, cid, cid)
                chunks_procesados.append({"id": cid, "tipo": tipo_str, **chunk_dict})
                idx_global += 1

        # Flush final
        if vectors_batch:
            pinecone_index.upsert(vectors=vectors_batch)
            logger.info(f"Batch final subido. Total chunks procesados: {idx_global}")

    return idx_global


def main():
    parser = argparse.ArgumentParser(description="Pipeline de ingesta para tutor de lógica")
    parser.add_argument("--libros",  required=True, nargs="+", help="Rutas a los PDFs de los libros")
    parser.add_argument("--excel",   required=True, help="Ruta al Excel con los temas del curso")
    parser.add_argument("--limpiar", action="store_true", help="Limpiar Neo4j antes de iniciar")
    args = parser.parse_args()

    # ── Credenciales desde variables de entorno ───────────────
    openai_key  = os.environ["OPENAI_API_KEY"]
    pinecone_key= os.environ["PINECONE_API_KEY"]
    neo4j_uri   = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
    neo4j_user  = os.environ.get("NEO4J_USER",     "neo4j")
    neo4j_pass  = os.environ.get("NEO4J_PASSWORD",  "tutor123")

    openai_client = OpenAI(api_key=openai_key)
    llm           = LLMExtractor(api_key=openai_key)
    pinecone_idx  = setup_pinecone(pinecone_key)

    Path("output").mkdir(exist_ok=True)
    chunks_procesados = []

    with Neo4jBuilder(neo4j_uri, neo4j_user, neo4j_pass) as neo4j:
        if args.limpiar:
            neo4j.limpiar_todo()
            logger.info("Limpiando índice Pinecone...")
            pinecone_idx.delete(delete_all=True)
            logger.info("Índice Pinecone limpiado.")
        neo4j.crear_indices()

        # ── 1. Grafo de prerrequisitos desde Excel ────────────
        generar_grafo_desde_excel(args.excel, neo4j)

        # ── 2. Procesar cada libro ────────────────────────────
        totales = []
        for i, ruta_pdf in enumerate(args.libros, start=1):
            fuente = f"libro_{i}"
            n = procesar_libro(
                ruta_pdf, fuente,
                llm, openai_client, pinecone_idx, neo4j, chunks_procesados
            )
            totales.append((fuente, n))

        logger.info(f"\n{'='*50}")
        logger.info(f"INGESTA COMPLETADA")
        for fuente, n in totales:
            logger.info(f"  {fuente}: {n} chunks")
        logger.info(f"  Total:   {sum(n for _, n in totales)} chunks en Pinecone y Neo4j")
        logger.info(f"  {llm.reporte_costo()}")
        logger.info(f"{'='*50}")

    # Guardar todos los chunks para auditoría
    with open("output/chunks_procesados.json", "w", encoding="utf-8") as f:
        json.dump(chunks_procesados, f, ensure_ascii=False, indent=2)
    logger.info("Chunks guardados en output/chunks_procesados.json")


if __name__ == "__main__":
    main()
