from sqlalchemy.orm import Session
from models import StudentProgress, Nivel
from services import neo4j_service

NIVEL_MAX_DIFICULTAD = {"BASICO": 1, "MEDIO": 2, "ALTO": 3}


def nivelar_unidad(unidad_id: str, nivel_str: str, student_id: int, db: Session) -> int:
    """
    Marca como dominados todos los nodos de unidad_id con dificultad <= nivel.
    Retorna la cantidad de nodos marcados.
    """
    unidad_num = int(unidad_id.split("_")[1])
    max_dif = NIVEL_MAX_DIFICULTAD.get(nivel_str, 1)
    nodos = neo4j_service.get_nodos_unidad(unidad_num, max_dif)

    for nodo in nodos:
        node_id = nodo["tema_canonico"]
        progress = db.query(StudentProgress).filter(
            StudentProgress.student_id == student_id,
            StudentProgress.node_id == node_id,
        ).first()

        if progress:
            progress.dominado = True
            progress.p_dominio = max(progress.p_dominio, 0.8)
            if not progress.nivel_confirmado:
                progress.nivel_confirmado = Nivel(nivel_str)
        else:
            db.add(StudentProgress(
                student_id=student_id,
                node_id=node_id,
                dominado=True,
                p_dominio=0.8,
                nivel_confirmado=Nivel(nivel_str),
            ))

    db.commit()
    return len(nodos)
