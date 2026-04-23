from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserRegistrationRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str | None = None
    role: UserRole | None = None


class GoogleAuthURLResponse(BaseModel):
    authorization_url: str
    state: str


class GoogleCallbackRequest(BaseModel):
    code: str
    state: str
