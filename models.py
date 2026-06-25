import enum
from sqlalchemy import (
    Column, Integer, String, DateTime, Float, Boolean,
    Enum as SAEnum, ForeignKey, JSON,
)
from sqlalchemy.sql import func
from database import Base


class Nivel(str, enum.Enum):
    BASICO = "BASICO"
    MEDIO = "MEDIO"
    ALTO = "ALTO"


class Rol(str, enum.Enum):
    ADMIN = "ADMIN"
    ESTUDIANTE = "ESTUDIANTE"
    PROFESOR = "PROFESOR"


class User(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    fecha_registro = Column(DateTime(timezone=True), server_default=func.now())
    unidad_actual = Column(Integer, default=1)
    nivel_actual = Column(SAEnum(Nivel), default=Nivel.BASICO)
    rol = Column(SAEnum(Rol), default=Rol.ESTUDIANTE)


class DiagnosticoSesion(Base):
    __tablename__ = "diagnostico_sesiones"

    id = Column(String, primary_key=True)
    student_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    unidad_id = Column(String, nullable=False)
    estado = Column(String, default="en_progreso")
    fecha_inicio = Column(DateTime(timezone=True), server_default=func.now())
    fecha_fin = Column(DateTime(timezone=True), nullable=True)
    nivel_resultado_global = Column(SAEnum(Nivel), nullable=True)
    respuestas_json = Column(JSON, default=list)


class BKTEstado(Base):
    __tablename__ = "bkt_estados"

    id = Column(Integer, primary_key=True, index=True)
    sesion_id = Column(String, ForeignKey("diagnostico_sesiones.id"), nullable=False)
    kc_dominio = Column(String, nullable=False)
    p_dominio = Column(Float, default=0.2)
    nivel_actual = Column(SAEnum(Nivel), default=Nivel.BASICO)
    preguntas_respondidas = Column(Integer, default=0)
    confirmadas_correctas = Column(Integer, default=0)
    completado = Column(Boolean, default=False)
    nivel_confirmado = Column(SAEnum(Nivel), nullable=True)
