"""
Cliente GPT-4o para extracción de imágenes y estructuración de texto.
Maneja reintentos, validación Pydantic y logging de costos estimados.
"""

import json
import time
import logging
from openai import OpenAI, RateLimitError
from pydantic import ValidationError

from prompts.extraction_prompts import (
    SYSTEM_BASE,
    PROMPT_CLASIFICAR_IMAGEN,
    build_prompt_texto,
    build_prompt_ejercicio,
    build_prompt_enunciado_img,
    build_prompt_diagrama,
)
from schemas.chunk_schema import (
    ChunkTexto, ChunkEjercicioResuelto,
    ChunkImagenEnunciado, ChunkDiagrama,
)
from extractors.pdf_extractor import ImagenExtraida

logger = logging.getLogger(__name__)

# Modelos
MODEL_VISION = "gpt-4o"          # único con soporte de imágenes
MODEL_TEXTO  = "gpt-4.1"         # más barato para texto plano

# Estimado de tokens por imagen (para logging de costo)
TOKENS_IMG_INPUT_EST  = 1000
TOKENS_IMG_OUTPUT_EST = 400


class LLMExtractor:
    """
    Llama a GPT-4o Vision para clasificar y extraer imágenes.
    Llama a GPT-4.1 para estructurar chunks de texto.
    """

    def __init__(self, api_key: str, max_retries: int = 3):
        self.client      = OpenAI(api_key=api_key)
        self.max_retries = max_retries
        self.tokens_usados_vision = 0
        self.tokens_usados_texto  = 0

    # ──────────────────────────────────────────────────────────
    # TEXTO PLANO
    # ──────────────────────────────────────────────────────────

    def estructurar_chunk_texto(self, texto: str, fuente_libro: str,
                                 pagina: int, capitulo: str = "",
                                 seccion: str = "") -> ChunkTexto | None:
        """Envía un chunk de texto a GPT-4.1 y retorna un ChunkTexto validado."""
        prompt = build_prompt_texto(texto, fuente_libro, pagina, capitulo, seccion)
        raw = self._llamar_texto(prompt)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            data["fuente_libro"] = fuente_libro
            data["pagina"]       = pagina
            return ChunkTexto(**data)
        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            logger.warning(f"Error validando chunk texto p.{pagina}: {e}")
            return None

    # ──────────────────────────────────────────────────────────
    # IMÁGENES
    # ──────────────────────────────────────────────────────────

    def procesar_imagen(self, imagen: ImagenExtraida,
                         fuente_libro: str,
                         enunciado_previo: str = ""):
        """
        Pipeline de imagen en dos pasos:
        1. Clasificar qué tipo de imagen es.
        2. Extraer el contenido según el tipo.
        Retorna el chunk correspondiente o None si no es relevante.
        """

        # ── Paso 1: Clasificar ────────────────────────────────
        clasificacion = self._clasificar_imagen(imagen)
        if not clasificacion or clasificacion == "otro":
            logger.info(f"Imagen p.{imagen.pagina} idx.{imagen.indice} → ignorada ({clasificacion})")
            return None

        logger.info(f"Imagen p.{imagen.pagina} idx.{imagen.indice} → {clasificacion}")

        # ── Paso 2: Extraer según tipo ────────────────────────
        if clasificacion == "ejercicio_resuelto":
            return self._extraer_ejercicio_resuelto(imagen, fuente_libro, enunciado_previo)

        elif clasificacion == "enunciado_imagen":
            return self._extraer_enunciado_imagen(imagen, fuente_libro)

        elif clasificacion == "diagrama":
            return self._extraer_diagrama(imagen, fuente_libro)

        return None

    def _clasificar_imagen(self, imagen: ImagenExtraida) -> str | None:
        prompt = PROMPT_CLASIFICAR_IMAGEN.format(
            texto_antes=imagen.texto_antes[:300],
            caption=imagen.caption,
            pagina=imagen.pagina,
        )
        raw = self._llamar_vision(prompt, imagen)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data.get("clasificacion", "otro")
        except json.JSONDecodeError:
            return None

    def _extraer_ejercicio_resuelto(self, imagen: ImagenExtraida,
                                     fuente_libro: str,
                                     enunciado_previo: str) -> ChunkEjercicioResuelto | None:
        prompt = build_prompt_ejercicio(enunciado_previo, imagen.caption, fuente_libro, imagen.pagina)
        raw = self._llamar_vision(prompt, imagen)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            data["fuente_libro"] = fuente_libro
            data["pagina"]       = imagen.pagina
            return ChunkEjercicioResuelto(**data)
        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            logger.warning(f"Error validando ejercicio_resuelto p.{imagen.pagina}: {e}")
            return None

    def _extraer_enunciado_imagen(self, imagen: ImagenExtraida,
                                   fuente_libro: str) -> ChunkImagenEnunciado | None:
        prompt = build_prompt_enunciado_img(imagen.caption, fuente_libro, imagen.pagina)
        raw = self._llamar_vision(prompt, imagen)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            data["fuente_libro"] = fuente_libro
            data["pagina"]       = imagen.pagina
            return ChunkImagenEnunciado(**data)
        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            logger.warning(f"Error validando enunciado_imagen p.{imagen.pagina}: {e}")
            return None

    def _extraer_diagrama(self, imagen: ImagenExtraida,
                           fuente_libro: str) -> ChunkDiagrama | None:
        prompt = build_prompt_diagrama(imagen.texto_antes, imagen.caption, fuente_libro, imagen.pagina)
        raw = self._llamar_vision(prompt, imagen)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            data["fuente_libro"] = fuente_libro
            data["pagina"]       = imagen.pagina
            return ChunkDiagrama(**data)
        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            logger.warning(f"Error validando diagrama p.{imagen.pagina}: {e}")
            return None

    # ──────────────────────────────────────────────────────────
    # LLAMADAS A LA API con reintentos
    # ──────────────────────────────────────────────────────────

    def _llamar_texto(self, prompt: str) -> str | None:
        """Llama a GPT-4.1 para texto plano."""
        for intento in range(self.max_retries):
            try:
                resp = self.client.chat.completions.create(
                    model=MODEL_TEXTO,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_BASE},
                        {"role": "user",   "content": prompt},
                    ],
                )
                self.tokens_usados_texto += resp.usage.total_tokens
                return resp.choices[0].message.content

            except RateLimitError:
                espera = 60 * (intento + 1)
                logger.warning(f"Rate limit alcanzado. Esperando {espera}s (intento {intento+1})...")
                time.sleep(espera)
            except Exception as e:
                logger.warning(f"Error GPT-4.1 intento {intento+1}: {e}")
                time.sleep(2 ** intento)

        return None

    def _llamar_vision(self, prompt: str, imagen: ImagenExtraida) -> str | None:
        """Llama a GPT-4o Vision con imagen en base64."""
        for intento in range(self.max_retries):
            try:
                resp = self.client.chat.completions.create(
                    model=MODEL_VISION,
                    temperature=0,
                    max_tokens=1500,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": SYSTEM_BASE},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{imagen.media_type};base64,{imagen.imagen_base64}",
                                        "detail": "high",
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        },
                    ],
                )
                self.tokens_usados_vision += resp.usage.total_tokens
                return resp.choices[0].message.content

            except RateLimitError:
                espera = 60 * (intento + 1)
                logger.warning(f"Rate limit alcanzado. Esperando {espera}s (intento {intento+1})...")
                time.sleep(espera)
            except Exception as e:
                logger.warning(f"Error GPT-4o Vision intento {intento+1}: {e}")
                time.sleep(2 ** intento)

        return None

    def reporte_costo(self) -> dict:
        """Estimado de costo en USD basado en tokens usados."""
        costo_texto  = (self.tokens_usados_texto  / 1_000_000) * 2.0   # GPT-4.1 input
        costo_vision = (self.tokens_usados_vision / 1_000_000) * 2.5   # GPT-4o input
        return {
            "tokens_texto":  self.tokens_usados_texto,
            "tokens_vision": self.tokens_usados_vision,
            "costo_estimado_usd": round(costo_texto + costo_vision, 4),
        }
