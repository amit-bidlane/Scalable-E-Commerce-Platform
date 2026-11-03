import pytest

from app import cli
from app.models.user import User, UserRole
from tests.test_services_auth import FakeSession


def make_customer() -> User:
    return User(email="boss@example.com", hashed_password="x", role=UserRole.CUSTOMER)


@pytest.mark.asyncio
async def test_create_admin_creates_an_admin(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(cli, "get_password_hash", lambda password: f"hashed::{password}")

    user = await cli.create_admin("boss@example.com", "Password123", False, session)

    assert user.role == UserRole.ADMIN
    assert user.hashed_password == "hashed::Password123"
    assert session.added is user
    assert session.committed is True


@pytest.mark.asyncio
async def test_create_admin_refuses_duplicate_email() -> None:
    existing = make_customer()
    session = FakeSession(existing_user=existing)

    with pytest.raises(ValueError, match="already exists"):
        await cli.create_admin("boss@example.com", "Password123", False, session)

    assert existing.role == UserRole.CUSTOMER
    assert session.committed is False


@pytest.mark.asyncio
async def test_create_admin_rejects_short_password() -> None:
    session = FakeSession()

    with pytest.raises(ValueError, match="at least 8"):
        await cli.create_admin("boss@example.com", "short", False, session)

    assert session.added is None
    assert session.committed is False


@pytest.mark.asyncio
async def test_promote_makes_existing_user_an_admin() -> None:
    existing = make_customer()
    session = FakeSession(existing_user=existing)

    user = await cli.create_admin("boss@example.com", None, True, session)

    assert user.role == UserRole.ADMIN
    assert session.committed is True


@pytest.mark.asyncio
async def test_promote_unknown_email_creates_nothing() -> None:
    session = FakeSession()

    with pytest.raises(ValueError, match="does not exist"):
        await cli.create_admin("ghost@example.com", None, True, session)

    assert session.added is None
    assert session.committed is False
