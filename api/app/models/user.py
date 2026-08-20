"""Modèle utilisateur."""

from datetime import datetime, timezone
from typing import Optional
from pydantic import EmailStr
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True, nullable=False)
    # Nullable en base : les comptes créés avant l'ajout de ce champ n'ont
    # pas de nom — required uniquement côté UserCreate pour les nouvelles
    # inscriptions.
    name: Optional[str] = Field(default=None, nullable=True)
    hashed_password: str
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Schémas Pydantic pour les requêtes/réponses API (jamais exposer hashed_password) ---

class UserCreate(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8)


class UserRead(SQLModel):
    id: int
    email: str
    name: Optional[str] = None
    is_active: bool
    is_admin: bool = False
    created_at: datetime


class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"
