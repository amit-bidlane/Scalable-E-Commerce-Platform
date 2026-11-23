"""Checkout (POST /api/v1/orders) re-prices the cart from the products and reserves stock.

Real cart API, real route, real PostgreSQL (same TEST_* settings and fixtures as test_regressions.py).
The payment provider is faked by patching payments._create_provider_order; no provider SDK is called.

Contract under test:
  - every line is charged the product's CURRENT price, not the price stored when it entered the cart;
  - the stock of every line is lowered in the same transaction as the order, or nothing happens;
  - not enough stock: 409 {"detail": {"message": "Not enough stock", "items": [{product_id, requested,
    available}, ...]}}, no order, stock and cart unchanged;
  - cancel_order gives back exactly what checkout took;
  - the request and the 201 response keep their shape.

What the guard tests protect:
  - test_checkout_response_keeps_its_shape: the 201 response keeps exactly its keys, so adding the
    Idempotency-Key header later does not change what clients receive.
  - test_a_provider_failure_... and test_a_provider_timeout_...: the provider call runs inside the
    transaction, so a provider error (an HTTPException, or an exception the app does not catch, which the
    test client turns into a 500) leaves no order, no reservation and the cart as it was.
  - test_checkout_locks_the_products_in_id_order: checkout locks the product rows FOR UPDATE, in id order.
    This is the test that guards the lock order. Its probe uses FOR NO KEY UPDATE, so the key-share lock of
    the order item foreign key cannot satisfy it.
  - test_checkouts_with_the_same_products_in_opposite_cart_order_do_not_deadlock: a timing-based check that
    parallel checkouts with the same products in opposite cart order all succeed. It can only show a
    deadlock that happens to occur; a pass does not prove none is possible. In the mutation check that
    locked the products in cart order it did not fail (3 runs of 10 rounds), while the lock-order test did.
"""

import asyncio
import itertools
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models.order import Order, OrderItem

# The database fixtures live in test_regressions.py; the cart and cancel helpers in their own test files.
from tests.test_cart import add, get_cart, make_product, money, new_user, set_price
from tests.test_order_cancel import cancel, stocks, warm_pool  # noqa: F401
from tests.test_regressions import client, engine, maker  # noqa: F401

ORDERS = "/api/v1/orders"
SHIPPING = "12 Test Street"
ORDER_KEYS = {
    "id",
    "status",
    "payment_provider",
    "provider_order_id",
    "provider_payment_id",
    "total_amount",
    "shipping_address",
}
RESPONSE_KEYS = {
    "order",
    "provider",
    "provider_order_id",
    "amount",
    "currency",
    "client_secret",
    "publishable_key",
    "razorpay_key_id",
}
buyers = itertools.count(1)


# -------------------------------------------------------------------------- helpers


class FakeProvider:
    """Stands in for payments._create_provider_order. Records what it was asked to charge."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, *, provider: str, order) -> dict:
        if self.error is not None:
            raise self.error
        self.calls.append({"provider": provider, "order_id": order.id, "amount": order.total_amount})
        return {"provider_order_id": f"order_fake_{order.id}"}


@pytest.fixture(autouse=True)
def provider(monkeypatch) -> FakeProvider:
    from app.services import payments

    fake = FakeProvider()
    monkeypatch.setattr(payments, "_create_provider_order", fake)
    return fake


async def buyer(client, lines: list[tuple[int, int]]) -> dict:
    """A new user whose cart holds the given (product_id, quantity) lines, added in that order."""
    headers = await new_user(client, f"checkout-buyer-{next(buyers)}@example.com")
    for product_id, quantity in lines:
        response = await add(client, headers, product_id, quantity)
        assert response.status_code == 200, response.text
    return headers


async def checkout(client, headers):
    return await client.post(ORDERS, headers=headers, json={"shipping_address": SHIPPING, "provider": "razorpay"})


async def set_stock(maker, product_id: int, stock: int) -> None:
    async with maker() as session:
        await session.execute(text("UPDATE products SET inventory_count = :n WHERE id = :id"), {"n": stock, "id": product_id})
        await session.commit()


async def order_count(maker) -> int:
    async with maker() as session:
        return (await session.execute(select(func.count()).select_from(Order))).scalar_one()


async def order_lines(maker, order_id: int) -> dict[int, tuple[int, Decimal]]:
    """{product_id: (quantity, unit_price)} of the stored order items."""
    async with maker() as session:
        rows = (
            await session.execute(
                select(OrderItem.product_id, OrderItem.quantity, OrderItem.unit_price).where(
                    OrderItem.order_id == order_id
                )
            )
        ).all()
    return {product_id: (quantity, unit_price) for product_id, quantity, unit_price in rows}


async def order_total_and_status(maker, order_id: int) -> tuple[Decimal, str]:
    async with maker() as session:
        row = (await session.execute(select(Order.total_amount, Order.status).where(Order.id == order_id))).one()
    return row.total_amount, row.status.value


# ------------------------------------------------------------------------ price


async def test_checkout_charges_the_current_price_after_the_product_price_changes(client, maker, provider) -> None:
    first = await make_product(maker, price="10.00", stock=10)
    second = await make_product(maker, price="3.00", stock=5)
    headers = await buyer(client, [(first, 2), (second, 1)])
    await set_price(maker, first, "12.50")  # after the line entered the cart at 10.00

    response = await checkout(client, headers)

    assert response.status_code == 201, response.text
    body = response.json()
    order_id = body["order"]["id"]
    assert money(body["amount"]) == Decimal("28.00")  # 2 x 12.50 + 1 x 3.00, not 2 x 10.00 + 1 x 3.00
    assert money(body["order"]["total_amount"]) == Decimal("28.00")
    assert await order_lines(maker, order_id) == {first: (2, Decimal("12.50")), second: (1, Decimal("3.00"))}
    assert (await order_total_and_status(maker, order_id))[0] == Decimal("28.00")
    assert [call["amount"] for call in provider.calls] == [Decimal("28.00")]


# ------------------------------------------------------------------- stock and cancel


async def test_checkout_lowers_the_stock_and_cancel_order_gives_it_back(client, maker) -> None:
    first = await make_product(maker, stock=10)
    second = await make_product(maker, stock=5)
    headers = await buyer(client, [(first, 3), (second, 2)])
    assert await stocks(maker, [first, second]) == [10, 5]
    cart_before = await get_cart(client, headers)

    response = await checkout(client, headers)

    assert response.status_code == 201, response.text
    assert await stocks(maker, [first, second]) == [7, 3]
    assert await get_cart(client, headers) == cart_before  # checkout does not clear the cart
    order_id = response.json()["order"]["id"]

    assert await cancel(maker, order_id) is True

    assert await stocks(maker, [first, second]) == [10, 5]
    assert (await order_total_and_status(maker, order_id))[1] == "CANCELLED"


async def test_checkout_works_when_the_stock_equals_the_quantity(client, maker) -> None:
    product = await make_product(maker, stock=3)
    headers = await buyer(client, [(product, 3)])

    response = await checkout(client, headers)

    assert response.status_code == 201, response.text
    assert await stocks(maker, [product]) == [0]


async def test_checkout_response_keeps_its_shape(client, maker, provider) -> None:
    product = await make_product(maker, price="10.00", stock=5)
    headers = await buyer(client, [(product, 2)])

    response = await checkout(client, headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert set(body["order"]) == ORDER_KEYS
    assert body["order"]["status"] == "PENDING"
    assert body["order"]["payment_provider"] == "razorpay"
    assert body["order"]["shipping_address"] == SHIPPING
    assert body["provider"] == "razorpay"
    assert body["currency"] == "INR"
    assert body["provider_order_id"] == body["order"]["provider_order_id"] == f"order_fake_{body['order']['id']}"
    assert money(body["amount"]) == money(body["order"]["total_amount"]) == Decimal("20.00")


# ----------------------------------------------------------------- not enough stock


async def test_checkout_without_enough_stock_is_409_names_the_short_items_and_changes_nothing(
    client, maker, provider
) -> None:
    fine = await make_product(maker, stock=10)
    short_by_one = await make_product(maker, stock=5)
    sold_out = await make_product(maker, stock=4)
    headers = await buyer(client, [(fine, 3), (short_by_one, 2), (sold_out, 4)])
    await set_stock(maker, short_by_one, 1)  # other buyers took the stock after this cart was filled
    await set_stock(maker, sold_out, 0)
    cart_before = await get_cart(client, headers)

    response = await checkout(client, headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["message"] == "Not enough stock"
    assert sorted(detail["items"], key=lambda item: item["product_id"]) == [
        {"product_id": short_by_one, "requested": 2, "available": 1},
        {"product_id": sold_out, "requested": 4, "available": 0},
    ]
    assert await order_count(maker) == 0
    assert await stocks(maker, [fine, short_by_one, sold_out]) == [10, 1, 0]  # the fine line is not reserved either
    assert await get_cart(client, headers) == cart_before
    assert provider.calls == []


# --------------------------------------------------------------------- concurrency


async def test_two_checkouts_for_the_last_unit_let_exactly_one_through(client, maker) -> None:
    await warm_pool(maker)
    product = await make_product(maker, stock=1)
    first = await buyer(client, [(product, 1)])
    second = await buyer(client, [(product, 1)])

    responses = await asyncio.gather(checkout(client, first), checkout(client, second))

    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    assert await stocks(maker, [product]) == [0]
    assert await order_count(maker) == 1


async def test_twenty_checkouts_for_five_units_let_exactly_five_through(client, maker) -> None:
    await warm_pool(maker)
    product = await make_product(maker, stock=5)
    users = [await buyer(client, [(product, 1)]) for _ in range(20)]

    responses = await asyncio.gather(*(checkout(client, headers) for headers in users))

    assert sorted(r.status_code for r in responses) == [201] * 5 + [409] * 15, [r.status_code for r in responses]
    assert await stocks(maker, [product]) == [0]  # never negative
    assert await order_count(maker) == 5


async def test_checkouts_with_the_same_products_in_opposite_cart_order_do_not_deadlock(client, maker) -> None:
    """Best effort: ten rounds of two buyers whose carts list the same two products in opposite order.
    A deadlock would show up as a 500 here. A pass does not prove a deadlock is impossible;
    test_checkout_locks_the_products_in_id_order checks the lock order itself."""
    await warm_pool(maker)
    for _ in range(10):
        low = await make_product(maker, stock=50)
        high = await make_product(maker, stock=50)
        forward = await buyer(client, [(low, 1), (high, 1)])
        backward = await buyer(client, [(high, 1), (low, 1)])

        responses = await asyncio.gather(checkout(client, forward), checkout(client, backward))

        assert [r.status_code for r in responses] == [201, 201], [r.text for r in responses]
        assert await stocks(maker, [low, high]) == [48, 48]


async def test_checkout_locks_the_products_in_id_order(client, maker) -> None:
    """The cart lists the higher product first. A checkout that has to wait for the higher product must
    already hold the lower one: it locks in id order, not in cart order.

    This shows the lock order for one checkout. It cannot show that no deadlock is possible; cancel_order
    and checkout must use the same order, which test_cancel_locks_the_products_in_id_order checks.

    The lower product's row is rewritten first, so its newest row version sits after the higher one on
    disk, and a query that reads the table in physical order would reach the higher product first. That
    depends on how Postgres plans the query, so it is best-effort: it catches a wrong order, and only
    possibly the absence of an explicit order.

    The probe uses FOR NO KEY UPDATE. That conflicts with the checkout's FOR UPDATE, but not with the
    FOR KEY SHARE that inserting an order item takes on its product through the foreign key. A FOR UPDATE
    probe would also fail because of that foreign key lock, and the test would then pass even when the
    checkout did not lock the products itself."""
    low = await make_product(maker, stock=7)
    high = await make_product(maker, stock=7)
    headers = await buyer(client, [(high, 1), (low, 1)])
    async with maker() as session:
        await session.execute(text("UPDATE products SET inventory_count = inventory_count WHERE id = :id"), {"id": low})
        await session.commit()

    async with maker() as holder:
        await holder.execute(text("SELECT id FROM products WHERE id = :id FOR UPDATE"), {"id": high})
        task = asyncio.create_task(checkout(client, headers))
        try:
            await asyncio.sleep(0.5)
            assert not task.done()  # waiting for the product the holder has locked

            async with maker() as probe:
                # NOWAIT: fails only if the lower product is already locked FOR UPDATE by the checkout. The
                # key-share lock of the order item foreign key does not conflict with FOR NO KEY UPDATE.
                with pytest.raises(DBAPIError):
                    await probe.execute(
                        text("SELECT id FROM products WHERE id = :id FOR NO KEY UPDATE NOWAIT"), {"id": low}
                    )
        finally:
            # Even when an assertion above fails: free the lock and let the checkout finish. A checkout
            # left waiting would be cancelled in the middle of its transaction and block the next test.
            await holder.rollback()
            response = await task

    assert response.status_code == 201, response.text
    assert await stocks(maker, [low, high]) == [6, 6]


# ------------------------------------------------------------------- provider failure


async def test_a_provider_failure_leaves_no_order_and_no_reservation(client, maker, provider) -> None:
    first = await make_product(maker, stock=10)
    second = await make_product(maker, stock=5)
    headers = await buyer(client, [(first, 3), (second, 2)])
    provider.error = HTTPException(status_code=503, detail="Razorpay is not configured.")
    cart_before = await get_cart(client, headers)

    response = await checkout(client, headers)

    assert response.status_code == 503, response.text
    assert await order_count(maker) == 0
    assert await stocks(maker, [first, second]) == [10, 5]
    assert await get_cart(client, headers) == cart_before


async def test_a_provider_timeout_leaves_no_order_and_no_reservation(client, maker, provider) -> None:
    """A plain exception, not an HTTPException (what the Razorpay SDK raises on a timeout). The app does not
    catch it. The test client does not re-raise it: ASGITransport(raise_app_exceptions=False) in the client
    fixture turns it into a 500 response. The session is closed without a commit, so everything rolls back."""
    from requests.exceptions import Timeout

    first = await make_product(maker, stock=10)
    second = await make_product(maker, stock=5)
    headers = await buyer(client, [(first, 3), (second, 2)])
    provider.error = Timeout("the provider took too long")
    cart_before = await get_cart(client, headers)

    response = await checkout(client, headers)

    assert response.status_code == 500, response.text
    assert await order_count(maker) == 0
    assert await stocks(maker, [first, second]) == [10, 5]
    assert await get_cart(client, headers) == cart_before
