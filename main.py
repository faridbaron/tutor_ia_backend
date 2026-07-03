"""
API FastAPI para el Tutor Inteligente.
Incluye autenticación JWT y el pipeline de ingesta (solo ADMIN).
"""

import os
import re
import unicodedata
import asyncio
import logging
import shutil
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import json

from database import engine, Base
import models
from routers.auth import router as auth_router
from routers.diagnostico_router import router as diagnostico_router
from routers.ruta_router import router as ruta_router
from routers.tutor_router import router as tutor_router
from routers.estudio_router import router as estudio_router
from auth import require_admin

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tutor API")

_frontend_url = os.getenv("FRONTEND_URL", "")
_allowed_origins = ["http://localhost:5173", "http://localhost:3000"]
if _frontend_url:
    _allowed_origins.append(_frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(diagnostico_router)
app.include_router(ruta_router)
app.include_router(tutor_router)
app.include_router(estudio_router)

# ── Configuración ──────────────────────────────────────────────
UPLOAD_DIR = Path(__file__).parent / "uploads"
EXCEL_PATH = Path(__file__).parent / "data" / "temas.xlsx"
UPLOAD_DIR.mkdir(exist_ok=True)

# ── Estado global del pipeline ─────────────────────────────────
pipeline_state = {
    "running": False,
    "logs": [],
    "pdfs": [],
}

log_queue: asyncio.Queue = None


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/status")
def get_status():
    return {
        "running": pipeline_state["running"],
        "pdfs": pipeline_state["pdfs"],
        "excel": str(EXCEL_PATH) if EXCEL_PATH.exists() else None,
    }


@app.post("/upload-pdfs")
async def upload_pdfs(
    files: list[UploadFile] = File(...),
    _admin: models.User = Depends(require_admin),
):
    if pipeline_state["running"]:
        raise HTTPException(status_code=409, detail="El pipeline ya está en ejecución.")

    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    pipeline_state["pdfs"] = []

    for file in files:
        if not file.filename.endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"{file.filename} no es un PDF.")
        dest = UPLOAD_DIR / file.filename
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        pipeline_state["pdfs"].append(file.filename)

    return {"uploaded": pipeline_state["pdfs"]}


@app.post("/run-grafo")
async def run_grafo(
    background_tasks: BackgroundTasks,
    _admin: models.User = Depends(require_admin),
):
    """Regenera solo el grafo de temas desde el Excel sin tocar Pinecone ni Chunks."""
    if pipeline_state["running"]:
        raise HTTPException(status_code=409, detail="El pipeline ya está en ejecución.")
    if not EXCEL_PATH.exists():
        raise HTTPException(status_code=400, detail=f"Excel no encontrado en {EXCEL_PATH}")

    global log_queue
    log_queue = asyncio.Queue()
    pipeline_state["logs"] = []
    pipeline_state["running"] = True

    background_tasks.add_task(_ejecutar_solo_grafo, asyncio.get_event_loop())
    return {"status": "iniciado"}


@app.post("/run")
async def run_pipeline(
    background_tasks: BackgroundTasks,
    limpiar: bool = False,
    _admin: models.User = Depends(require_admin),
):
    if pipeline_state["running"]:
        raise HTTPException(status_code=409, detail="El pipeline ya está en ejecución.")
    if not pipeline_state["pdfs"]:
        raise HTTPException(status_code=400, detail="No hay PDFs cargados.")
    if not EXCEL_PATH.exists():
        raise HTTPException(status_code=400, detail=f"Excel no encontrado en {EXCEL_PATH}")

    global log_queue
    log_queue = asyncio.Queue()
    pipeline_state["logs"] = []
    pipeline_state["running"] = True

    background_tasks.add_task(_ejecutar_pipeline, limpiar, asyncio.get_event_loop())
    return {"status": "iniciado"}


@app.get("/logs")
async def stream_logs():
    """SSE público: transmite los logs del pipeline en tiempo real."""
    async def event_generator() -> AsyncGenerator[str, None]:
        for entry in pipeline_state["logs"]:
            yield f"data: {json.dumps(entry)}\n\n"

        while pipeline_state["running"] or (log_queue and not log_queue.empty()):
            try:
                entry = await asyncio.wait_for(log_queue.get(), timeout=1.0)
                pipeline_state["logs"].append(entry)
                yield f"data: {json.dumps(entry)}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── Lógica del pipeline (corre en thread para no bloquear el event loop) ──────

async def _ejecutar_solo_grafo(loop: asyncio.AbstractEventLoop):
    def put(entry):
        loop.call_soon_threadsafe(log_queue.put_nowait, entry)

    class ThreadSafeQueueHandler(logging.Handler):
        def emit(self, record):
            try:
                put({"type": "log", "message": self.format(record)})
            except Exception:
                pass

    handler = ThreadSafeQueueHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    def run_sync():
        try:
            from ingesta import generar_grafo_desde_excel
            from pipeline.neo4j_builder import Neo4jBuilder

            neo4j_uri  = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
            neo4j_user = os.environ.get("NEO4J_USER",     "neo4j")
            neo4j_pass = os.environ.get("NEO4J_PASSWORD", "tutor123")

            with Neo4jBuilder(neo4j_uri, neo4j_user, neo4j_pass) as neo4j:
                neo4j.crear_indices()
                # Generar grafo desde Excel (produce el dict con nodos y relaciones)
                # y luego migrar usando regenerar_grafo_temas
                from ingesta import _generar_grafo_dict
                grafo = _generar_grafo_dict(str(EXCEL_PATH))
                neo4j.regenerar_grafo_temas(grafo)

            put({"type": "done", "resumen": {"grafo": "ok"}})
        except Exception as e:
            logging.exception(f"Error en run-grafo: {e}")
            put({"type": "error", "message": str(e)})
        finally:
            pipeline_state["running"] = False
            root_logger.removeHandler(handler)

    await asyncio.to_thread(run_sync)


async def _ejecutar_pipeline(limpiar: bool, loop: asyncio.AbstractEventLoop):
    def put(entry):
        loop.call_soon_threadsafe(log_queue.put_nowait, entry)

    class ThreadSafeQueueHandler(logging.Handler):
        def emit(self, record):
            try:
                put({"type": "log", "message": self.format(record)})
            except Exception:
                pass

    handler = ThreadSafeQueueHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    def run_sync():
        try:
            from ingesta import (
                setup_pinecone, generar_grafo_desde_excel,
                procesar_libro, LLMExtractor,
            )
            from openai import OpenAI
            from pipeline.neo4j_builder import Neo4jBuilder

            openai_key   = os.environ["OPENAI_API_KEY"]
            pinecone_key = os.environ["PINECONE_API_KEY"]
            neo4j_uri    = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
            neo4j_user   = os.environ.get("NEO4J_USER",     "neo4j")
            neo4j_pass   = os.environ.get("NEO4J_PASSWORD", "tutor123")

            openai_client = OpenAI(api_key=openai_key)
            llm           = LLMExtractor(api_key=openai_key)
            pinecone_idx  = setup_pinecone(pinecone_key)

            Path("output").mkdir(exist_ok=True)
            chunks_procesados = []
            pdfs = [str(UPLOAD_DIR / nombre) for nombre in pipeline_state["pdfs"]]

            with Neo4jBuilder(neo4j_uri, neo4j_user, neo4j_pass) as neo4j:
                if limpiar:
                    neo4j.limpiar_todo()
                    try:
                        logging.warning("Limpiando índice Pinecone...")
                        pinecone_idx.delete(delete_all=True, namespace="")
                        logging.warning("Índice Pinecone limpiado.")
                    except Exception as e_pine:
                        logging.warning(f"Pinecone delete: {e_pine} (continuando)")
                neo4j.crear_indices()

                with neo4j.driver.session() as s:
                    n_temas = s.run("MATCH (t:Tema) RETURN count(t) AS n").single()["n"]
                if n_temas == 0:
                    generar_grafo_desde_excel(str(EXCEL_PATH), neo4j)
                else:
                    logging.warning(f"Grafo ya existe ({n_temas} temas). Saltando generación.")

                totales = []
                for ruta_pdf in pdfs:
                    nombre = Path(ruta_pdf).stem
                    fuente = re.sub(r'[^\w]', '_', unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii")).strip('_')
                    n = procesar_libro(
                        ruta_pdf, fuente,
                        llm, openai_client, pinecone_idx, neo4j, chunks_procesados
                    )
                    totales.append((fuente, n))

                put({
                    "type": "resumen",
                    "libros": [{"fuente": f, "chunks": n} for f, n in totales],
                    "total": sum(n for _, n in totales),
                    "costo": llm.reporte_costo(),
                })

        except Exception as e:
            put({"type": "error", "message": str(e)})
        finally:
            pipeline_state["running"] = False
            root_logger.removeHandler(handler)

    await asyncio.to_thread(run_sync)
