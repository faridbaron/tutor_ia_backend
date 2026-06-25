"""
Schemas Pydantic para los chunks del tutor de lógica y pensamiento computacional.
Todos los chunks se validan antes de ir a Pinecone y Neo4j.
"""

from pydantic import BaseModel, Field, BeforeValidator
from typing import Optional, Literal, Annotated
from enum import Enum

def _to_int(v):
    return int(v) if v is not None else None

OptionalInt = Annotated[Optional[int], BeforeValidator(_to_int)]


class TipoContenido(str, Enum):
    DEFINICION       = "definicion"        # concepto teórico explicado
    EJEMPLO_RESUELTO = "ejemplo_resuelto"  # código PSeInt con ejecución
    ENUNCIADO        = "enunciado"         # ejercicio propuesto sin solución
    DIAGRAMA         = "diagrama"          # diagrama de flujo u otro visual
    TEORIA           = "teoria"            # texto explicativo sin código


class NivelDificultad(int, Enum):
    BASICO      = 1
    INTERMEDIO  = 2
    AVANZADO    = 3


class EjemploEjecucion(BaseModel):
    entradas: list[str] = Field(default_factory=list, description="Valores ingresados por el usuario en el ejemplo")
    salidas:  list[str] = Field(default_factory=list, description="Resultados que imprime el algoritmo")


class ChunkTexto(BaseModel):
    """Chunk extraído de texto plano del libro (PyMuPDF directo)."""
    tipo:                TipoContenido
    tema_canonico:       str   = Field(description="Nombre normalizado del tema. Ej: 'bucles_mientras'")
    subtema:             Optional[str] = None
    contenido:           str   = Field(description="Texto completo del chunk")
    conceptos:           list[str] = Field(description="Conceptos de programación involucrados")
    prerequisitos:       list[str] = Field(description="Temas que el estudiante debe conocer antes")
    dificultad:          NivelDificultad
    fuente_libro:        str
    pagina:              int
    capitulo:            Optional[str] = None
    seccion:             Optional[str] = None


class ChunkImagenEnunciado(BaseModel):
    """Enunciado de ejercicio presentado como imagen en el libro."""
    tipo:                Literal[TipoContenido.ENUNCIADO] = TipoContenido.ENUNCIADO
    tema_canonico:       str
    subtema:             Optional[str] = None
    enunciado_texto:     str  = Field(description="Texto extraído de la imagen del enunciado")
    conceptos:           list[str]
    prerequisitos:       list[str]
    dificultad:          NivelDificultad
    fuente_libro:        str
    pagina:              int
    figura:              OptionalInt = None
    caption:             Optional[str] = None


class ChunkEjercicioResuelto(BaseModel):
    """Código PSeInt con su ejecución, extraído de imagen con GPT-4o Vision."""
    tipo:                Literal[TipoContenido.EJEMPLO_RESUELTO] = TipoContenido.EJEMPLO_RESUELTO
    tema_canonico:       str
    subtema:             Optional[str] = None
    enunciado:           Optional[str] = Field(None, description="Enunciado del ejercicio si aparece en texto cercano")
    enunciado_es_imagen: bool = False
    codigo_pseint:       str  = Field(description="Pseudocódigo PSeInt transcrito exactamente")
    descripcion:         str  = Field(description="Qué hace el algoritmo en lenguaje natural")
    ejemplo_ejecucion:   Optional[EjemploEjecucion] = None
    conceptos:           list[str]
    prerequisitos:       list[str]
    dificultad:          NivelDificultad
    fuente_libro:        str
    pagina:              int
    figura:              OptionalInt = None
    caption:             Optional[str] = None


class ChunkDiagrama(BaseModel):
    """Diagrama de flujo u otro visual pedagógico."""
    tipo:                Literal[TipoContenido.DIAGRAMA] = TipoContenido.DIAGRAMA
    tema_canonico:       str
    descripcion_visual:  str  = Field(description="Descripción detallada de lo que muestra el diagrama")
    tipo_diagrama:       str  = Field(description="Ej: 'diagrama_de_flujo', 'tabla_de_verdad', 'mapa_conceptual'")
    conceptos:           list[str]
    prerequisitos:       list[str]
    dificultad:          NivelDificultad
    fuente_libro:        str
    pagina:              int
    figura:              OptionalInt = None
    caption:             Optional[str] = None
