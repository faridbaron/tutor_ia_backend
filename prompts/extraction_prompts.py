"""
Prompts para el pipeline de ingesta del tutor de lógica y pensamiento computacional.
Todos los prompts están en español y orientados a estudiantes de primer semestre universitario.
"""

# ─────────────────────────────────────────────────────────────
# SISTEMA BASE (común a todos los prompts de texto)
# ─────────────────────────────────────────────────────────────

SYSTEM_BASE = """
Eres un asistente especializado en procesar libros de texto universitarios de 
Lógica y Pensamiento Computacional para primer semestre universitario.

Los estudiantes NO tienen experiencia previa en programación.

Tu tarea es extraer y estructurar el contenido en JSON válido para alimentar 
una base de conocimientos con Pinecone (búsqueda semántica) y Neo4j (grafo de prerrequisitos).

REGLAS CRÍTICAS:
- Responde ÚNICAMENTE con JSON válido, sin texto adicional, sin bloques de código markdown.
- El campo tema_canonico DEBE ser uno de los valores exactos de esta lista (copia exactamente):
  "que_es_un_algoritmo", "propiedades_finitud_precision_efectividad",
  "estructura_entradaprocesosalida", "ciclo_de_vida_de_un_programa",
  "diferencia_entre_algoritmo_y_programa", "diferencia_compilador_vs_interprete",
  "tipos_de_datos_basicos_entero_real_texto", "variables_y_asignacion_de_valores",
  "operadores_aritmeticos", "operadores_relacionales", "operadores_logicos_and_or_not",
  "jerarquia_de_operadores", "simbolos_del_diagrama_de_flujo",
  "diseo_de_algoritmos_con_estructura_secuencial", "reglas_para_disear_diagramas_de_flujo",
  "expresiones_algoritmicas_compuestas", "prueba_de_escritorio",
  "identificar_secuencias_en_ejemplos_simples", "detectar_repeticiones_en_un_proceso",
  "identificar_entradas_y_salidas_de_un_problema", "pasos_para_analizar_un_enunciado",
  "tablas_de_verdad_and_or_not", "expresiones_booleanas_simples",
  "decision_simple_if", "decision_compuesta_ifelse", "decisiones_anidadas",
  "decisiones_multiples_switch_else_if", "condiciones_compuestas_and_or_combinados",
  "cortocircuito_logico", "evaluacion_de_expresiones_booleanas_complejas",
  "que_es_un_ciclo_y_para_que_sirve", "concepto_de_contador", "concepto_de_acumulador",
  "ciclo_for_estructura_basica", "ciclo_while_estructura_y_condicion_de_parada",
  "ciclo_dowhile_y_cuando_usarlo", "diferencias_entre_for_while_y_dowhile",
  "contadores_y_acumuladores_dentro_de_ciclos", "ciclos_con_condiciones_compuestas",
  "uso_de_contadores_para_contar_ocurrencias", "acumulacion_de_resultados_en_ciclos",
  "generalizar_soluciones_para_distintos_datos", "definicion_de_una_funcion",
  "parametros_de_entrada_de_una_funcion", "llamado_de_funciones",
  "funciones_que_retornan_un_valor", "division_de_un_problema_en_subproblemas",
  "logica_condicional_integrada_con_funciones", "validacion_de_datos_de_entrada",
  "manejo_de_casos_borde_y_excepciones_logicas",
  "definicion_y_estructura_de_un_vector", "acceso_a_elementos_por_indice",
  "recorrido_basico_de_un_vector_con_ciclo", "ciclos_con_vectores_llenar_buscar_modificar",
  "busqueda_de_elementos_en_un_vector", "identificar_el_maximo_minimo_y_promedio_en_arreglos",
  "modularidad_disear_multiples_funciones_relacionadas",
  "reutilizacion_de_funciones_en_distintos_contextos",
  "funciones_que_llaman_a_otras_funciones", "diseo_topdown_de_una_solucion_compleja",
  "definicion_y_estructura_de_una_matriz", "acceso_a_celdas_por_fila_y_columna",
  "modificacion_de_contenido_de_vectores_y_matrices",
  "ciclos_anidados_doble_for_para_matrices", "ciclos_con_matrices_recorrido_completo",
  "combinacion_de_ciclos_con_funciones", "busqueda_en_matrices_fila_y_columna",
  "patrones_en_matrices_diagonal_bordes_simetria",
  "generalizacion_de_algoritmos_para_n_dimensiones",
  "representacion_de_datos_del_mundo_real_en_arreglos",
  "trazabilidad_de_un_algoritmo_paso_a_paso", "diseo_de_algoritmos_de_alta_complejidad",
  "optimizacion_de_un_algoritmo", "comparar_soluciones_alternativas_al_mismo_problema",
  "diseo_completo_de_un_caso_de_estudio",
  "optimizacion_de_ciclos_evitar_iteraciones_innecesarias"

- El campo nivel_tema DEBE ser: "BASICO", "MEDIO" o "ALTO" según la complejidad del contenido.
  BASICO = definición y reconocimiento, MEDIO = aplicación, ALTO = evaluación y creación.
  El node_id en Neo4j será: tema_canonico + "_" + nivel_tema. Ej: "que_es_un_algoritmo_BASICO"

- prerequisitos: lista de node_id completos (tema_canonico + nivel).
  Ej: ["que_es_un_algoritmo_BASICO", "variables_y_asignacion_de_valores_BASICO"]

- conceptos: lista de conceptos específicos del chunk.
  Ej: ["acumulador", "contador", "variable_centinela"]
""".strip()


# ─────────────────────────────────────────────────────────────
# PROMPT 1: CHUNK DE TEXTO PLANO
# ─────────────────────────────────────────────────────────────

PROMPT_CHUNK_TEXTO = """
Analiza el siguiente fragmento de texto de un libro de Lógica y Pensamiento Computacional.

METADATOS DE CONTEXTO:
- Libro: {fuente_libro}
- Capítulo: {capitulo}
- Sección: {seccion}
- Página: {pagina}

TEXTO DEL CHUNK:
\"\"\"
{texto}
\"\"\"

Devuelve un JSON con esta estructura exacta:
{{
  "tipo": "<definicion|teoria|enunciado>",
  "tema_canonico": "<nombre en snake_case>",
  "subtema": "<subtema específico o null>",
  "contenido": "<texto completo limpio y sin artefactos de PDF>",
  "conceptos": ["<concepto1>", "<concepto2>"],
  "prerequisitos": ["<tema_canonico1>", "<tema_canonico2>"],
  "dificultad": <1|2|3>,
  "fuente_libro": "{fuente_libro}",
  "pagina": {pagina},
  "capitulo": "{capitulo}",
  "seccion": "{seccion}"
}}

Si el chunk es un enunciado de ejercicio propuesto sin solución, usa tipo "enunciado".
Si es explicación teórica con definición formal, usa "definicion".
Si es texto explicativo sin definición formal, usa "teoria".
""".strip()


# ─────────────────────────────────────────────────────────────
# PROMPT 2: CLASIFICADOR DE IMAGEN
# ─────────────────────────────────────────────────────────────

PROMPT_CLASIFICAR_IMAGEN = """
Analiza esta imagen de un libro de Lógica y Pensamiento Computacional en PSeInt.

CONTEXTO:
- Texto antes de la imagen: "{texto_antes}"
- Caption/pie de figura: "{caption}"
- Página: {pagina}

Clasifica la imagen en UNA de estas categorías y devuelve JSON:

{{
  "clasificacion": "<ejercicio_resuelto|enunciado_imagen|diagrama|otro>",
  "razon": "<explicación breve de por qué>"
}}

Definiciones:
- ejercicio_resuelto: contiene código PSeInt (con palabras como Algoritmo, Leer, Imprimir, FinAlgoritmo), 
  con o sin recuadro de ejecución/salida.
- enunciado_imagen: texto de un problema o ejercicio propuesto sin código de solución.
- diagrama: diagrama de flujo, tabla de verdad, mapa conceptual u otro visual esquemático.
- otro: imagen decorativa, foto, o contenido no pedagógico relevante.
""".strip()


# ─────────────────────────────────────────────────────────────
# PROMPT 3: EXTRACCIÓN DE EJERCICIO RESUELTO (imagen con código PSeInt)
# ─────────────────────────────────────────────────────────────

PROMPT_EJERCICIO_RESUELTO = """
Esta imagen contiene un ejercicio resuelto en PSeInt de un libro universitario 
de Lógica y Pensamiento Computacional.

CONTEXTO:
- Enunciado del ejercicio (texto antes de la imagen): "{enunciado_previo}"
- Caption: "{caption}"
- Libro: {fuente_libro}
- Página: {pagina}

Extrae TODO el contenido y devuelve este JSON exacto:
{{
  "tipo": "ejemplo_resuelto",
  "tema_canonico": "<tema en snake_case>",
  "subtema": "<subtema específico o null>",
  "enunciado": "<enunciado del ejercicio si lo encuentras en el contexto, o null>",
  "enunciado_es_imagen": false,
  "codigo_pseint": "<transcripción EXACTA del pseudocódigo PSeInt, preservando indentación con espacios>",
  "descripcion": "<descripción en lenguaje natural de qué hace el algoritmo y cómo lo resuelve>",
  "ejemplo_ejecucion": {{
    "entradas": ["<valor1>", "<valor2>"],
    "salidas": ["<linea_salida1>", "<linea_salida2>"]
  }},
  "conceptos": ["<concepto1>", "<concepto2>"],
  "prerequisitos": ["<tema_canonico1>"],
  "dificultad": <1|2|3>,
  "fuente_libro": "{fuente_libro}",
  "pagina": {pagina},
  "figura": <número de figura o null>,
  "caption": "{caption}"
}}

IMPORTANTE para codigo_pseint:
- Transcribe el código EXACTAMENTE como aparece, respetando palabras clave de PSeInt.
- Usa "Algoritmo", "FinAlgoritmo", "Leer", "Imprimir", "Si", "Entonces", "SiNo", 
  "FinSi", "Mientras", "Hacer", "FinMientras", "Para", "FinPara", "Repetir", "HastaQue".
- Preserva la indentación usando 4 espacios por nivel.
- Si hay recuadro de "Ejecución", extrae los valores de entradas y salidas en ejemplo_ejecucion.
- Si no hay recuadro de ejecución, deja ejemplo_ejecucion como null.
""".strip()


# ─────────────────────────────────────────────────────────────
# PROMPT 4: EXTRACCIÓN DE ENUNCIADO EN IMAGEN
# ─────────────────────────────────────────────────────────────

PROMPT_ENUNCIADO_IMAGEN = """
Esta imagen contiene el enunciado de un ejercicio propuesto (sin solución) 
de un libro de Lógica y Pensamiento Computacional.

CONTEXTO:
- Caption: "{caption}"
- Libro: {fuente_libro}
- Página: {pagina}

Extrae el contenido y devuelve este JSON:
{{
  "tipo": "enunciado",
  "tema_canonico": "<tema en snake_case>",
  "subtema": "<subtema o null>",
  "enunciado_texto": "<transcripción completa y exacta del texto del enunciado>",
  "conceptos": ["<concepto1>"],
  "prerequisitos": ["<tema_canonico1>"],
  "dificultad": <1|2|3>,
  "fuente_libro": "{fuente_libro}",
  "pagina": {pagina},
  "figura": <número o null>,
  "caption": "{caption}"
}}
""".strip()


# ─────────────────────────────────────────────────────────────
# PROMPT 5: EXTRACCIÓN DE DIAGRAMA
# ─────────────────────────────────────────────────────────────

PROMPT_DIAGRAMA = """
Esta imagen contiene un diagrama de un libro de Lógica y Pensamiento Computacional.

CONTEXTO:
- Texto antes de la imagen: "{texto_antes}"
- Caption: "{caption}"
- Libro: {fuente_libro}
- Página: {pagina}

Describe el diagrama y devuelve este JSON:
{{
  "tipo": "diagrama",
  "tema_canonico": "<tema en snake_case>",
  "descripcion_visual": "<descripción detallada de qué muestra el diagrama, qué elementos tiene, cómo se conectan, qué enseña>",
  "tipo_diagrama": "<diagrama_de_flujo|tabla_de_verdad|mapa_conceptual|pseudocodigo_visual|otro>",
  "conceptos": ["<concepto1>"],
  "prerequisitos": ["<tema_canonico1>"],
  "dificultad": <1|2|3>,
  "fuente_libro": "{fuente_libro}",
  "pagina": {pagina},
  "figura": <número o null>,
  "caption": "{caption}"
}}
""".strip()


# ─────────────────────────────────────────────────────────────
# PROMPT 6: GRAFO DE PRERREQUISITOS (Neo4j)
# Se usa UNA SOLA VEZ con la lista de temas del Excel
# ─────────────────────────────────────────────────────────────

PROMPT_GRAFO_PREREQUISITOS = """
Eres un experto en diseño curricular de cursos de Lógica y Pensamiento Computacional 
para estudiantes universitarios de primer semestre sin experiencia en programación.

Se te da la lista de temas del curso:
{lista_temas}

Genera el grafo de prerrequisitos completo en JSON. Para cada tema indica 
qué otros temas del curso son prerrequisito DIRECTO (inmediato anterior necesario).

Devuelve ÚNICAMENTE este JSON:
{{
  "nodos": [
    {{
      "tema_canonico": "<snake_case>",
      "nombre_display": "<nombre legible para mostrar al estudiante>",
      "descripcion": "<qué aprende el estudiante en este tema>",
      "dificultad": <1|2|3>,
      "tiempo_estimado_horas": <número>,
      "unidad": "<nombre de la unidad o módulo del curso>"
    }}
  ],
  "relaciones": [
    {{
      "desde": "<tema_canonico prerequisito>",
      "hacia": "<tema_canonico que lo requiere>",
      "tipo": "REQUIERE_PREVIO"
    }}
  ]
}}

REGLAS:
- tema_canonico debe coincidir EXACTAMENTE con los usados en los chunks.
- Solo incluye prerrequisitos DIRECTOS, no transitivos.
- Un tema puede tener múltiples prerrequisitos.
- El tema inicial (sin prerrequisitos) debe tener relaciones vacías como "desde".
""".strip()


# ─────────────────────────────────────────────────────────────
# HELPER: construir prompt de texto con contexto
# ─────────────────────────────────────────────────────────────

def build_prompt_texto(texto: str, fuente_libro: str, pagina: int,
                        capitulo: str = "", seccion: str = "") -> str:
    return PROMPT_CHUNK_TEXTO.format(
        texto=texto,
        fuente_libro=fuente_libro,
        pagina=pagina,
        capitulo=capitulo or "desconocido",
        seccion=seccion or "desconocida",
    )


def build_prompt_ejercicio(enunciado_previo: str, caption: str,
                            fuente_libro: str, pagina: int) -> str:
    return PROMPT_EJERCICIO_RESUELTO.format(
        enunciado_previo=enunciado_previo or "No disponible",
        caption=caption or "",
        fuente_libro=fuente_libro,
        pagina=pagina,
    )


def build_prompt_enunciado_img(caption: str, fuente_libro: str, pagina: int) -> str:
    return PROMPT_ENUNCIADO_IMAGEN.format(
        caption=caption or "",
        fuente_libro=fuente_libro,
        pagina=pagina,
    )


def build_prompt_diagrama(texto_antes: str, caption: str,
                           fuente_libro: str, pagina: int) -> str:
    return PROMPT_DIAGRAMA.format(
        texto_antes=texto_antes or "No disponible",
        caption=caption or "",
        fuente_libro=fuente_libro,
        pagina=pagina,
    )
