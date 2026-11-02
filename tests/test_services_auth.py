import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError

from app.models.user import User, UserRole
from app.schemas.auth import UserRegistrationRequest
from app.services import auth as auth_service


class ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, existing_user=None):
        self.existing_user = existing_user
        self.added = None
        self.committed = False
        self.refreshed = False

    async def execute(self, query):
        return ScalarResult(self.existing_user)

    def add(self, obj):
        self.added = obj
        if getattr(obj, "id", None) is None:
            obj.id = 101

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        self.refreshed = True


class FakeRedisCache:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def set_if_absent(self, key, value, ttl_seconds=None):
        self.calls.append(key)
        if self.error is not None:
            raise self.error
        return True


@pytest.mark.asyncio
async def test_register_user_creates_customer_by_default(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(auth_service, "get_password_hash", lambda password: f"hashed::{password}")

    user = await auth_service.register_user(
        UserRegistrationRequest(email="customer@example.com", password="Password123"),
        session,
    )

    assert user.email == "customer@example.com"
    assert user.role == UserRole.CUSTOMER
    assert user.hashed_password == "hashed::Password123"
    assert session.committed is True


@pytest.mark.asyncio
async def test_register_user_rejects_duplicate_email() -> None:
    session = FakeSession(existing_user=User(id=1, email="exists@example.com", hashed_password="hashed"))

    with pytest.raises(HTTPException) as exc:
        await auth_service.register_user(
            UserRegistrationRequest(email="exists@example.com", password="Password123"),
            session,
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_authenticate_user_rejects_bad_password(monkeypatch) -> None:
    session = FakeSession(existing_user=User(id=1, email="user@example.com", hashed_password="hashed"))
    monkeypatch.setattr(auth_service, "verify_password", lambda plain, hashed: False)

    with pytest.raises(HTTPException) as exc:
        await auth_service.authenticate_user("user@example.com", "bad-pass", session)

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_access_token_rejects_invalid_token(monkeypatch) -> None:
    def fake_decode_token(token, secret_key):
        raise ValueError("Invalid token")

    monkeypatch.setattr(auth_service, "decode_token", fake_decode_token)

    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token("bad-token")

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_access_token_rejects_token_without_jti(monkeypatch) -> None:
    cache = FakeRedisCache()
    monkeypatch.setattr(auth_service, "decode_token", lambda token, secret_key: {"type": "refresh", "sub": "1", "exp": 4102444800})
    monkeypatch.setattr(auth_service, "redis_cache", cache)

    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token("token-without-jti")

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid refresh token."
    assert cache.calls == []


@pytest.mark.asyncio
async def test_refresh_access_token_returns_503_when_redis_is_down(monkeypatch) -> None:
    cache = FakeRedisCache(error=RedisError("down"))
    monkeypatch.setattr(auth_service, "decode_token", lambda token, secret_key: {"type": "refresh", "sub": "1", "jti": "abc", "exp": 4102444800})
    monkeypatch.setattr(auth_service, "redis_cache", cache)

    with pytest.raises(HTTPException) as exc:
        await auth_service.refresh_access_token("any-token")

    assert exc.value.status_code == 503
    assert exc.value.detail == "Cannot refresh right now. Please retry."
    assert cache.calls == ["auth:refresh_used:abc"]
