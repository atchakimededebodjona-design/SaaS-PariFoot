"""Endpoints d'authentification."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.rate_limit import limiter
from app.models.user import User, UserCreate, UserRead, Token
from app.auth.security import hash_password, authenticate_user, create_access_token, get_current_user
from app.auth.admin import _admin_emails

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
def register(request: Request, user_in: UserCreate, session: Session = Depends(get_session)):
    existing = session.exec(select(User).where(User.email == user_in.email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Un compte existe déjà avec cet email")

    user = User(email=user_in.email, hashed_password=hash_password(user_in.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.post("/login", response_model=Token)
@limiter.limit("30/minute")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    """
    Utilise OAuth2PasswordRequestForm (standard FastAPI) : le champ
    s'appelle `username` dans le formulaire même si on y met un email —
    c'est la convention OAuth2, pas un choix arbitraire. Ça permet aussi
    de tester directement depuis /docs (bouton "Authorize").

    Rate-limité à 30/minute par IP — assez pour un utilisateur qui se
    trompe de mot de passe plusieurs fois, pas assez pour du brute force.
    """
    user = authenticate_user(session, form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou mot de passe incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(subject=user.email)
    return Token(access_token=access_token)


@router.get("/me", response_model=UserRead)
def read_current_user(current_user: User = Depends(get_current_user)):
    return UserRead(
        id=current_user.id,
        email=current_user.email,
        is_active=current_user.is_active,
        is_admin=current_user.email.strip().lower() in _admin_emails(),
        created_at=current_user.created_at,
    )
