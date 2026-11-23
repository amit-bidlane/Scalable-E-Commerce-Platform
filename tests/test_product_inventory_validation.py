"""A negative inventory_count sent to the admin product routes is rejected by request validation (422),
before the database CHECK ck_products_inventory_non_negative could answer with a 500.

Against a real PostgreSQL, with the same TEST_* settings and fixtures as test_regressions.py.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

# The database fixtures live in test_regressions.py; importing them here avoids copying them.
from tests.test_regressions import client, engine, maker  # noqa: F401


@pytest.fixture
def as_admin():
    """Act as an admin. conftest.py clears app.dependency_overrides after every test."""
    from app.dependencies import get_current_user
    from app.main import app
    from app.models.user import User, UserRole

    async def admin():
        return User(id=1, email="admin@example.com", hashed_password="x", role=UserRole.ADMIN)

    app.dependency_overrides[get_current_user] = admin


async def test_create_rejects_a_negative_inventory_count_with_422(client, maker, as_admin) -> None:
    body = {"name": "P", "slug": "p", "sku": "P-1", "price": "10.00", "inventory_count": -1}

    response = await client.post("/api/v1/products", json=body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"][-1] == "inventory_count"


async def test_update_rejects_a_negative_inventory_count_with_422_and_keeps_the_stock(
    client, maker, as_admin
) -> None:
    from app.models.product import Product

    async with maker() as session:
        product = Product(name="P", slug="p", sku="P-1", price=Decimal("10.00"), inventory_count=3)
        session.add(product)
        await session.commit()
        product_id = product.id

    response = await client.put(f"/api/v1/products/{product_id}", json={"inventory_count": -1})

    assert response.status_code == 422, response.text
    async with maker() as session:
        stock = (await session.execute(select(Product.inventory_count).where(Product.id == product_id))).scalar_one()
    assert stock == 3
