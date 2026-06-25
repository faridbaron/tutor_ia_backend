"""
Extractor de contenido de PDFs usando PyMuPDF.
Separa bloques de texto e imágenes por página, detecta captions de figuras.
"""

import fitz  # PyMuPDF
import re
import base64
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImagenExtraida:
    pagina: int
    indice: int              # índice de la imagen en la página
    imagen_base64: str       # imagen codificada en base64 para GPT-4o Vision
    media_type: str          # "image/png" o "image/jpeg"
    texto_antes: str         # texto de la misma página antes de la imagen
    caption: str             # texto de pie de figura detectado


@dataclass
class BloqueTexto:
    pagina: int
    texto: str
    y_position: float        # posición vertical en la página (para ordenar)
    es_titulo: bool = False


@dataclass
class PaginaExtraida:
    numero: int
    bloques_texto: list[BloqueTexto] = field(default_factory=list)
    imagenes: list[ImagenExtraida] = field(default_factory=list)


class PDFExtractor:
    """
    Extrae texto e imágenes de un PDF académico preservando el orden de la página.
    Detecta automáticamente captions de figuras (patrón "Figura N.").
    """

    CAPTION_PATTERN = re.compile(
        r"(Figura\s+\d+[\.\-]?\s*.{0,150})", re.IGNORECASE
    )
    MIN_IMAGE_SIZE = 50   # ignorar imágenes menores a 50x50 px (íconos, decoración)

    def __init__(self, pdf_path: str, fuente_libro: str = "libro_1"):
        self.pdf_path   = Path(pdf_path)
        self.fuente     = fuente_libro
        self.documento  = None

    def __enter__(self):
        self.documento = fitz.open(str(self.pdf_path))
        return self

    def __exit__(self, *args):
        if self.documento:
            self.documento.close()

    def extraer_todo(self) -> list[PaginaExtraida]:
        """Procesa todas las páginas del PDF y retorna la lista de páginas extraídas."""
        paginas = []
        for num_pagina in range(len(self.documento)):
            pagina = self._procesar_pagina(num_pagina)
            paginas.append(pagina)
        return paginas

    def extraer_rango(self, inicio: int, fin: int) -> list[PaginaExtraida]:
        """Procesa solo un rango de páginas (útil para pruebas)."""
        paginas = []
        for num_pagina in range(inicio, min(fin, len(self.documento))):
            pagina = self._procesar_pagina(num_pagina)
            paginas.append(pagina)
        return paginas

    def _procesar_pagina(self, num_pagina: int) -> PaginaExtraida:
        pagina_fitz = self.documento[num_pagina]
        numero_real = num_pagina + 1  # páginas base-1

        # ── Extraer bloques de texto ──────────────────────────────
        bloques_texto = self._extraer_bloques_texto(pagina_fitz, numero_real)

        # ── Extraer imágenes ──────────────────────────────────────
        texto_completo_pagina = " ".join(b.texto for b in bloques_texto)
        imagenes = self._extraer_imagenes(
            pagina_fitz, numero_real, bloques_texto, texto_completo_pagina
        )

        return PaginaExtraida(
            numero=numero_real,
            bloques_texto=bloques_texto,
            imagenes=imagenes,
        )

    def _extraer_bloques_texto(self, pagina_fitz, numero_real: int) -> list[BloqueTexto]:
        bloques = []
        raw = pagina_fitz.get_text("blocks")  # [(x0,y0,x1,y1,text,block_no,block_type)]

        for bloque in raw:
            if bloque[6] != 0:   # solo bloques de texto (tipo 0), ignorar imágenes
                continue
            texto = bloque[4].strip()
            if not texto or len(texto) < 3:
                continue

            # Heurística simple para detectar títulos (texto corto en posición alta)
            es_titulo = len(texto) < 80 and bloque[1] < 150

            bloques.append(BloqueTexto(
                pagina=numero_real,
                texto=texto,
                y_position=bloque[1],
                es_titulo=es_titulo,
            ))

        # Ordenar por posición vertical
        bloques.sort(key=lambda b: b.y_position)
        return bloques

    def _extraer_imagenes(self, pagina_fitz, numero_real: int,
                           bloques: list[BloqueTexto],
                           texto_completo: str) -> list[ImagenExtraida]:
        imagenes = []
        imgs_raw = pagina_fitz.get_images(full=True)

        for idx, img_info in enumerate(imgs_raw):
            xref = img_info[0]
            try:
                img_dict = self.documento.extract_image(xref)
            except Exception:
                continue

            # Filtrar imágenes muy pequeñas
            if img_dict["width"] < self.MIN_IMAGE_SIZE or img_dict["height"] < self.MIN_IMAGE_SIZE:
                continue

            # Codificar en base64 para GPT-4o Vision
            img_bytes  = img_dict["image"]
            img_b64    = base64.b64encode(img_bytes).decode("utf-8")
            media_type = f"image/{img_dict['ext'].lower()}"
            if media_type == "image/jpg":
                media_type = "image/jpeg"

            # Detectar caption en el texto de la página
            caption = self._detectar_caption(texto_completo, idx)

            # Texto antes = todos los bloques de texto de la página
            texto_antes = " ".join(b.texto for b in bloques[:5])[:500]

            imagenes.append(ImagenExtraida(
                pagina=numero_real,
                indice=idx,
                imagen_base64=img_b64,
                media_type=media_type,
                texto_antes=texto_antes,
                caption=caption,
            ))

        return imagenes

    def _detectar_caption(self, texto_pagina: str, img_idx: int) -> str:
        """Busca el patrón 'Figura N.' en el texto de la página."""
        matches = self.CAPTION_PATTERN.findall(texto_pagina)
        if matches:
            # Si hay múltiples figuras en la página, intentar tomar la del mismo índice
            idx = min(img_idx, len(matches) - 1)
            return matches[idx].strip()
        return ""


# ─────────────────────────────────────────────────────────────
# Utilidad: agrupar bloques de texto en chunks semánticos
# ─────────────────────────────────────────────────────────────

def agrupar_en_chunks(paginas: list[PaginaExtraida],
                       max_chars: int = 1500) -> list[dict]:
    """
    Agrupa bloques de texto en chunks semánticos por sección.
    Un nuevo chunk comienza cuando aparece un título o se supera max_chars.
    """
    chunks = []
    buffer_texto = []
    buffer_pagina_inicio = None
    seccion_actual = ""
    capitulo_actual = ""

    for pagina in paginas:
        for bloque in pagina.bloques_texto:

            if bloque.es_titulo:
                # Guardar chunk anterior si tiene contenido
                if buffer_texto:
                    chunks.append({
                        "texto": " ".join(buffer_texto),
                        "pagina": buffer_pagina_inicio,
                        "capitulo": capitulo_actual,
                        "seccion": seccion_actual,
                    })
                    buffer_texto = []

                # Detectar si es capítulo o sección
                texto_lower = bloque.texto.lower()
                if any(p in texto_lower for p in ["capítulo", "capitulo", "unidad", "módulo", "modulo"]):
                    capitulo_actual = bloque.texto
                    seccion_actual  = ""
                else:
                    seccion_actual = bloque.texto

                buffer_pagina_inicio = bloque.pagina

            else:
                if buffer_pagina_inicio is None:
                    buffer_pagina_inicio = bloque.pagina

                buffer_texto.append(bloque.texto)

                # Si el buffer supera el límite, cerrar chunk
                if sum(len(t) for t in buffer_texto) > max_chars:
                    chunks.append({
                        "texto": " ".join(buffer_texto),
                        "pagina": buffer_pagina_inicio,
                        "capitulo": capitulo_actual,
                        "seccion": seccion_actual,
                    })
                    buffer_texto = []
                    buffer_pagina_inicio = None

    # Último chunk
    if buffer_texto:
        chunks.append({
            "texto": " ".join(buffer_texto),
            "pagina": buffer_pagina_inicio or 0,
            "capitulo": capitulo_actual,
            "seccion": seccion_actual,
        })

    return chunks
