from fastapi.testclient import TestClient

from app.main import app
from app.models.user import User, UserRole


def test_register_route_returns_token_pair(monkeypatch) -> None:
    async def fake_register_user(payload, session):
        return User(
            id=11,
            email=payload.email,
            hashed_password="hashed",
            full_name=payload.full_name,
            role=UserRole.CUSTOMER,
        )

    def fake_issue_token_pair(user):
        return {
            "access_token": f"access-{user.id}",
            "refresh_token": f"refresh-{user.id}",
            "token_type": "bearer",
        }

    monkeypatch.setattr("app.api.routes.auth.register_user", fake_register_user)
    monkeypatch.setattr("app.api.routes.auth.issue_token_pair", fake_issue_token_pair)

    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "StrongPass123",
            "full_name": "New User",
        },
    )

    assert response.status_code == 201
    assert response.json()["access_token"] == "access-11"


def test_login_route_returns_token_pair(monkeypatch) -> None:
    async def fake_authenticate_user(email, password, session):
        assert email == "user@example.com"
        assert password == "Password123"
        return User(
            id=7,
            email=email,
            hashed_password="hashed",
            role=UserRole.ADMIN,
        )

    def fake_issue_token_pair(user):
        return {
            "access_token": f"access-{user.role.value}",
            "refresh_token": "refresh-token",
            "token_type": "bearer",
        }

    monkeypatch.setattr("app.api.routes.auth.authenticate_user", fake_authenticate_user)
    monkeypatch.setattr("app.api.routes.auth.issue_token_pair", fake_issue_token_pair)

    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/login",
        data={"username": "user@example.com", "password": "Password123"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "access-admin"


def test_refresh_route_rotates_tokens(monkeypatch) -> None:
    async def fake_refresh_access_token(refresh_token):
        assert refresh_token == "refresh-token"
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
        }

    monkeypatch.setattr("app.api.routes.auth.refresh_access_token", fake_refresh_access_token)

    client = TestClient(app)
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "refresh-token"})

    assert response.status_code == 200
    assert response.json()["refresh_token"] == "new-refresh"


def test_google_login_route_returns_scaffold_url(monkeypatch) -> None:
    def fake_build_google_authorization_url():
        return "https://accounts.google.com/example", "state-123"

    monkeypatch.setattr("app.api.routes.auth.build_google_authorization_url", fake_build_google_authorization_url)

    client = TestClient(app)
    response = client.get("/api/v1/auth/google/login")

    assert response.status_code == 200
    assert response.json()["state"] == "state-123"


def test_google_callback_route_returns_scaffold_response(monkeypatch) -> None:
    def fake_exchange_google_code(code, state):
        return {"message": "Google OAuth scaffolding is ready.", "code": code, "state": state}

    monkeypatch.setattr("app.api.routes.auth.exchange_google_code", fake_exchange_google_code)

    client = TestClient(app)
    response = client.post("/api/v1/auth/google/callback", json={"code": "oauth-code", "state": "oauth-state"})

    assert response.status_code == 200
    assert response.json()["code"] == "oauth-code"
