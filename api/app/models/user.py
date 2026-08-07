"""Modèle utilisateur."""

from datetime import datetime, timezone
from typing import Optional
from pydantic import EmailStr
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True, nullable=False)
    hashed_password: str
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- Schémas Pydantic pour les requêtes/réponses API (jamais exposer hashed_password) ---

class UserCreate(SQLModel):
    email: EmailStr
    password: str = Field(min_length=8)


class UserRead(SQLModel):
    id: int
    email: str
    is_active: bool
    created_at: datetime


class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"
