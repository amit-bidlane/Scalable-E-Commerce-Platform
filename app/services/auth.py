from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.models.user import User, UserRole
from app.schemas.auth import TokenPair, UserRegistrationRequest


async def register_user(payload: UserRegistrationRequest, session: AsyncSession) -> User:
    existing = await session.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered.")

    user = User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
        full_name=payload.full_name,
        role=payload.role or UserRole.CUSTOMER,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate_user(email: str, password: str, session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def issue_token_pair(user: User) -> TokenPair:
    subject = str(user.id)
    access_token = create_access_token(subject=subject, extra_claims={"role": user.role.value})
    refresh_token = create_refresh_token(subject=subject)
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


def refresh_access_token(refresh_token: str) -> TokenPair:
    try:
        payload = decode_token(refresh_token, settings.jwt_refresh_secret_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.") from exc

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token type.")

    subject = payload.get("sub")
    if subject is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token subject is missing.")

    access_token = create_access_token(subject=subject)
    new_refresh_token = create_refresh_token(subject=subject)
    return TokenPair(access_token=access_token, refresh_token=new_refresh_token)


def build_google_authorization_url() -> tuple[str, str]:
    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured yet.",
        )

    state = uuid4().hex
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}", state


def exchange_google_code(code: str, state: str) -> dict[str, str]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured yet.",
        )

    return {
        "message": "Google OAuth scaffolding is ready. Token exchange and account linking can be completed next.",
        "code": code,
        "state": state,
    }
