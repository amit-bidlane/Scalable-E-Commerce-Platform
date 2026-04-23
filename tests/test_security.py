from fastapi.testclient import TestClient

from app.core.security import create_access_token, create_refresh_token
from app.dependencies import get_current_user
from app.main import app
from app.models.user import User, UserRole


class DummySession:
    async def execute(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("Session should not be used for forbidden admin operations.")


async def override_session():
    yield DummySession()


def test_refresh_token_pair_issued() -> None:
    access_token = create_access_token("1", extra_claims={"role": UserRole.ADMIN.value})
    refresh_token = create_refresh_token("1")

    assert access_token
    assert refresh_token
    assert access_token != refresh_token


def test_admin_product_route_rejects_customer() -> None:
    async def override_current_user() -> User:
        return User(
            id=1,
            email="customer@example.com",
            hashed_password="hashed",
            role=UserRole.CUSTOMER,
        )

    from app.db.session import get_db_session

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_db_session] = override_session

    client = TestClient(app)
    response = client.post(
        "/api/v1/products",
        json={
            "name": "Keyboard",
            "slug": "keyboard",
            "description": "Mechanical keyboard",
            "sku": "KB-001",
            "price": "99.99",
            "inventory_count": 5,
            "category_ids": [],
        },
        headers={"Authorization": "Bearer fake-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin privileges are required for this action."

    app.dependency_overrides.clear()
