"""
Script de uso único: cambia fuente_libro="libro_1" → "bronson"
en Pinecone (metadata) y en Neo4j (propiedades de nodos Chunk).
"""

import os
from pinecone import Pinecone
from neo4j import GraphDatabase

FUENTE_VIEJA = "libro_1"
FUENTE_NUEVA = "C++ para Ingeniería y Ciencias de Bronson"

PINECONE_INDEX = "tutor-logica-computacional"
NEO4J_URI  = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER",     "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "tutor123")


def actualizar_pinecone():
    pc  = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    idx = pc.Index(PINECONE_INDEX)

    print("Listando IDs de Pinecone...")
    ids = []
    for page in idx.list(prefix=FUENTE_VIEJA + "_"):
        for item in page:
            ids.append(item.id if hasattr(item, "id") else str(item))

    if not ids:
        print("No se encontraron vectores con ese prefijo.")
        return

    print(f"Encontrados {len(ids)} vectores. Actualizando metadata...")
    for i, vid in enumerate(ids, 1):
        idx.update(id=vid, set_metadata={"fuente_libro": FUENTE_NUEVA})
        if i % 50 == 0:
            print(f"  {i}/{len(ids)} actualizados...")

    print(f"Pinecone: {len(ids)} vectores actualizados.")


def actualizar_neo4j():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with driver.session() as session:
        result = session.run(
            "MATCH (c:Chunk {fuente_libro: $vieja}) "
            "SET c.fuente_libro = $nueva "
            "RETURN count(c) AS total",
            vieja=FUENTE_VIEJA, nueva=FUENTE_NUEVA,
        )
        total = result.single()["total"]
        print(f"Neo4j: {total} chunks actualizados.")
    driver.close()


if __name__ == "__main__":
    actualizar_pinecone()
    actualizar_neo4j()
    print("Listo.")
