from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
import models
import auth as auth_utils

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    nombre: str
    username: str
    password: str
    rol: models.Rol = models.Rol.ESTUDIANTE


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    nombre: str
    username: str
    rol: str
    unidad_actual: int
    nivel_actual: str
    fecha_registro: str

    class Config:
        from_attributes = True


@router.post("/register", response_model=UserResponse, status_code=201)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.username == req.username).first():
        raise HTTPException(status_code=400, detail="El alias ya está en uso")

    user = models.User(
        nombre=req.nombre,
        username=req.username,
        password_hash=auth_utils.hash_password(req.password),
        rol=req.rol,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize(user)


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()
    if not user or not auth_utils.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Alias o contraseña incorrectos")

    token = auth_utils.create_access_token({"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer", "user": _serialize(user)}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: models.User = Depends(auth_utils.get_current_user)):
    return _serialize(current_user)


def _serialize(user: models.User) -> dict:
    return {
        "id": user.id,
        "nombre": user.nombre,
        "username": user.username,
        "rol": user.rol.value if hasattr(user.rol, "value") else str(user.rol),
        "unidad_actual": user.unidad_actual,
        "nivel_actual": user.nivel_actual.value if hasattr(user.nivel_actual, "value") else str(user.nivel_actual),
        "fecha_registro": str(user.fecha_registro or ""),
    }
