from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limiter import limiter, public_rate_limit
from app.db.session import get_db_session
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.auth import (
    GoogleAuthURLResponse,
    GoogleCallbackRequest,
    RefreshTokenRequest,
    TokenPair,
    UserRegistrationRequest,
)
from app.services.auth import (
    authenticate_user,
    build_google_authorization_url,
    exchange_google_code,
    issue_token_pair,
    refresh_access_token,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="Register a Customer Account",
    description="Creates a new user account and immediately issues access and refresh tokens for authenticated sessions.",
    response_description="JWT access and refresh token pair for the newly registered user.",
)
@limiter.limit(public_rate_limit)
async def register(
    request: Request,
    payload: UserRegistrationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenPair:
    user = await register_user(payload, session)
    return issue_token_pair(user)


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Login with Email and Password",
    description="Authenticates a user via OAuth2 password flow and returns a fresh JWT token pair.",
    response_description="JWT access and refresh token pair for the authenticated user.",
)
@limiter.limit(public_rate_limit)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_db_session),
) -> TokenPair:
    user = await authenticate_user(form_data.username, form_data.password, session)
    return issue_token_pair(user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Refresh JWT Tokens",
    description="Validates a refresh token and rotates both the access and refresh tokens.",
    response_description="New JWT token pair after successful refresh validation.",
)
@limiter.limit(public_rate_limit)
async def refresh_token(request: Request, payload: RefreshTokenRequest) -> TokenPair:
    return refresh_access_token(payload.refresh_token)


@router.get(
    "/google/login",
    response_model=GoogleAuthURLResponse,
    summary="Start Google OAuth Login",
    description="Returns the Google authorization URL and anti-forgery state token for frontend redirect flow.",
    response_description="Google authorization URL and generated OAuth state value.",
)
@limiter.limit(public_rate_limit)
async def google_login(request: Request) -> GoogleAuthURLResponse:
    authorization_url, state = build_google_authorization_url()
    return GoogleAuthURLResponse(authorization_url=authorization_url, state=state)


@router.post(
    "/google/callback",
    summary="Handle Google OAuth Callback",
    description="Accepts the OAuth callback payload and exposes server-side scaffolding for token exchange and account linking.",
    response_description="Scaffolding acknowledgement for Google OAuth callback processing.",
)
@limiter.limit(public_rate_limit)
async def google_callback(request: Request, payload: GoogleCallbackRequest) -> dict[str, str]:
    return exchange_google_code(payload.code, payload.state)


@router.get(
    "/me",
    summary="Get Current User Profile",
    description="Returns the authenticated user's basic identity and role information from the access token context.",
    response_description="Authenticated user profile summary.",
)
async def read_current_user(current_user: User = Depends(get_current_user)) -> dict[str, str | int]:
    return {
        "id": current_user.id,
        "email": current_user.email,
        "role": current_user.role.value,
    }
