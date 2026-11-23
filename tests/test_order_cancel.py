"""Order cancellation, and a payment that arrives for a cancelled order (decision D2).

Against a real PostgreSQL and Redis, with the same TEST_* settings and fixtures as test_regressions.py.

Contract under test:
  cancel_order(session, order_id) -> bool   (app/services/orders.py)
    True  - the order was PENDING; it is now CANCELLED and the stock its items reserved is back.
    False - the order does not exist or is not PENDING (PAID, SHIPPED, CANCELLED); nothing changed.
    It commits with session.commit().
  A payment webhook for a CANCELLED order stores provider_payment_id and paid_at, keeps the status
  CANCELLED, logs at ERROR level (with order_id, provider_payment_id and amount_matches), publishes
  nothing, and answers 200. CANCELLED orders with paid_at set are the ones that need a refund.

Checkout does not reserve stock yet. The tests store the product stock already lowered by the order's
quantities, which is what checkout will leave behind; cancel_order must give it back.
"""

import asyncio
import itertools
import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

# The database fixtures and webhook helpers live in test_regressions.py; importing them avoids copying them.
from tests.test_regressions import (  # noqa: F401
    client,
    engine,
    live_redis,
    maker,
    order_status,
    post_webhook,
    published,
    razorpay_body,
    webhook_secret,
)

LINES = [(10, 3), (5, 2)]  # (stock before checkout, quantity ordered): 5 units of INR 100.00 = INR 500.00
BEFORE = [10, 5]
RESERVED = [7, 3]
PAISE = 50000
numbers = itertools.count(1)


# -------------------------------------------------------------------------- helpers


async def make_products(maker, stocks: list[int]) -> list[int]:
    """One product per stock value, created one at a time so the ids rise in list order."""
    from app.models.product import Product

    ids = []
    async with maker() as session:
        for stock in stocks:
            n = next(numbers)
            product = Product(
                name=f"Cancel product {n}",
                slug=f"cancel-product-{n}",
                sku=f"CANCEL-{n}",
                price=Decimal("100.00"),
                inventory_count=stock,
            )
            session.add(product)
            await session.flush()
            ids.append(product.id)
        await session.commit()
    return ids


async def make_order(maker, lines: list[tuple[int, int]], *, status: str = "PENDING", provider_order_id=None) -> int:
    """An order with items (product_id, quantity), stored in the given order. Every unit costs INR 100.00."""
    from app.models.order import Order, OrderItem, OrderState
    from app.models.user import User, UserRole

    n = next(numbers)
    async with maker() as session:
        user = User(email=f"cancel-buyer-{n}@example.com", hashed_password="x", role=UserRole.CUSTOMER)
        session.add(user)
        await session.flush()
        order = Order(
            user_id=user.id,
            status=getattr(OrderState, status),
            payment_provider="razorpay",
            total_amount=Decimal("100.00") * sum(quantity for _, quantity in lines),
            shipping_address="12 Test Street",
            provider_order_id=provider_order_id or f"order_cancel_{n}",
            items=[OrderItem(product_id=pid, quantity=qty, unit_price=Decimal("100.00")) for pid, qty in lines],
        )
        session.add(order)
        await session.commit()
        return order.id


async def make_reserved_order(maker, lines=LINES, **kwargs) -> tuple[int, list[int]]:
    """An order whose stock is already lowered by its quantities, as checkout will leave it."""
    product_ids = await make_products(maker, [stock - quantity for stock, quantity in lines])
    order_id = await make_order(
        maker, [(pid, quantity) for pid, (_, quantity) in zip(product_ids, lines)], **kwargs
    )
    return order_id, product_ids


async def stocks(maker, product_ids: list[int]) -> list[int]:
    from app.models.product import Product

    async with maker() as session:
        rows = (
            await session.execute(
                select(Product.id, Product.inventory_count).where(Product.id.in_(product_ids))
            )
        ).all()
    by_id = {product_id: count for product_id, count in rows}
    return [by_id[product_id] for product_id in product_ids]


async def order_row(maker, order_id: int) -> dict:
    from app.models.order import Order

    async with maker() as session:
        row = (
            await session.execute(
                select(Order.status, Order.provider_payment_id, Order.paid_at).where(Order.id == order_id)
            )
        ).one()
    return {"status": row.status.value, "provider_payment_id": row.provider_payment_id, "paid_at": row.paid_at}


async def refund_list(maker) -> list[int]:
    from app.models.order import Order, OrderState

    async with maker() as session:
        rows = await session.execute(
            select(Order.id).where(Order.status == OrderState.CANCELLED, Order.paid_at.is_not(None))
        )
        return [order_id for (order_id,) in rows]


def import_cancel_order():
    from app.services.orders import cancel_order

    return cancel_order


async def cancel(maker, order_id: int) -> bool:
    cancel_order = import_cancel_order()
    async with maker() as session:
        return await cancel_order(session, order_id)


async def warm_pool(maker, connections: int = 20) -> None:
    """Open the pool's connections first. With a cold pool the first request can finish before the
    others have connected, and the race a test is about never happens."""

    async def hold() -> None:
        async with maker() as session:
            await session.execute(text("SELECT pg_sleep(0.1)"))

    await asyncio.gather(*(hold() for _ in range(connections)))


class PauseBeforeCommit:
    """Wraps a session so a test can stop cancel_order right before it commits."""

    def __init__(self, session, reached: asyncio.Event, proceed: asyncio.Event) -> None:
        self._session = session
        self._reached = reached
        self._proceed = proceed

    async def commit(self) -> None:
        self._reached.set()
        await self._proceed.wait()
        await self._session.commit()

    def __getattr__(self, name):
        return getattr(self._session, name)


def error_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.payments" and r.levelno == logging.ERROR]


# --------------------------------------------------------------------------- cancel


async def test_cancelling_a_pending_order_returns_the_stock_of_both_products(maker) -> None:
    order_id, product_ids = await make_reserved_order(maker)
    assert await stocks(maker, product_ids) == RESERVED

    assert await cancel(maker, order_id) is True

    assert (await order_row(maker, order_id))["status"] == "CANCELLED"
    assert await stocks(maker, product_ids) == BEFORE


async def test_cancelling_one_order_leaves_other_orders_alone(maker) -> None:
    order_id, product_ids = await make_reserved_order(maker)
    other_id, other_products = await make_reserved_order(maker)

    assert await cancel(maker, order_id) is True

    assert (await order_row(maker, other_id))["status"] == "PENDING"
    assert await stocks(maker, other_products) == RESERVED
    assert await stocks(maker, product_ids) == BEFORE


async def test_cancelling_twice_returns_the_stock_once(maker) -> None:
    order_id, product_ids = await make_reserved_order(maker)

    first = await cancel(maker, order_id)
    second = await cancel(maker, order_id)

    assert (first, second) == (True, False)
    assert await stocks(maker, product_ids) == BEFORE  # not [13, 7]


async def test_20_parallel_cancels_return_the_stock_once(maker) -> None:
    order_id, product_ids = await make_reserved_order(maker)
    await warm_pool(maker)

    results = await asyncio.gather(*(cancel(maker, order_id) for _ in range(20)))

    assert sorted(results) == [False] * 19 + [True]
    assert (await order_row(maker, order_id))["status"] == "CANCELLED"
    assert await stocks(maker, product_ids) == BEFORE


@pytest.mark.parametrize("status", ["PAID", "SHIPPED"])
async def test_an_order_that_is_not_pending_cannot_be_cancelled(maker, status: str) -> None:
    order_id, product_ids = await make_reserved_order(maker, status=status)

    assert await cancel(maker, order_id) is False

    assert (await order_row(maker, order_id))["status"] == status
    assert await stocks(maker, product_ids) == RESERVED


async def test_cancelling_an_unknown_order_returns_false(maker) -> None:
    assert await cancel(maker, 999999) is False


# ------------------------------------------------------------------- product lock order


async def test_cancel_locks_the_products_in_id_order(maker) -> None:
    """The order's items are stored highest product id first. A cancel that has to wait for the higher
    product must already hold the lower one: it locks in id order, not in item order.

    This shows the lock order for one cancel. It cannot show that no deadlock is possible: other code
    that locks products (checkout in Task 3) has to use the same order.

    The lower product's row is rewritten first, so its newest row version sits after the higher one on
    disk, and a query that reads the table in physical order would reach the higher product first. That
    depends on how Postgres plans the query, so it is best-effort: it catches a wrong order, and only
    possibly the absence of an explicit order."""
    import_cancel_order()
    low, high = await make_products(maker, [7, 7])
    order_id = await make_order(maker, [(high, 1), (low, 1)])
    async with maker() as session:
        await session.execute(
            text("UPDATE products SET inventory_count = inventory_count WHERE id = :id"), {"id": low}
        )
        await session.commit()

    async with maker() as holder:
        await holder.execute(text("SELECT id FROM products WHERE id = :id FOR UPDATE"), {"id": high})
        task = asyncio.create_task(cancel(maker, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # waiting for the product the holder has locked

        async with maker() as probe:
            with pytest.raises(DBAPIError):  # NOWAIT: the lower product is already locked by the cancel
                await probe.execute(text("SELECT id FROM products WHERE id = :id FOR UPDATE NOWAIT"), {"id": low})
        await holder.rollback()

    assert await task is True
    assert await stocks(maker, [low, high]) == [8, 8]


async def test_parallel_cancels_of_orders_sharing_products_do_not_deadlock(maker) -> None:
    """Best effort: ten rounds of two orders that list the same two products in opposite order.
    A deadlock would show up as an exception here. A pass does not prove a deadlock is impossible;
    test_cancel_locks_the_products_in_id_order checks the lock order itself."""
    import_cancel_order()
    await warm_pool(maker)
    for _ in range(10):
        low, high = await make_products(maker, [98, 98])
        first = await make_order(maker, [(low, 1), (high, 1)])
        second = await make_order(maker, [(high, 1), (low, 1)])

        results = await asyncio.gather(cancel(maker, first), cancel(maker, second), return_exceptions=True)

        assert results == [True, True], results
        assert await stocks(maker, [low, high]) == [100, 100]


# ------------------------------------------------------------- cancel against a webhook


async def test_cancel_during_a_payment_webhook_is_refused_and_the_order_stays_paid(
    client, maker, live_redis, published, monkeypatch
) -> None:
    """The webhook holds the order row (SELECT ... FOR UPDATE) while it works. A cancel started in
    that moment must wait, then see PAID and refuse."""
    from app.services import payments

    import_cancel_order()
    resolve = payments._resolve_order_from_event
    locked = asyncio.Event()

    async def slow_resolve(*args, **kwargs):
        result = await resolve(*args, **kwargs)  # the order row is locked from here until the commit
        locked.set()
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(payments, "_resolve_order_from_event", slow_resolve)
    order_id, product_ids = await make_reserved_order(maker, provider_order_id="order_cancel_vs_webhook")
    body = razorpay_body(provider_order_id="order_cancel_vs_webhook", payment_id="pay_cw", amount_paise=PAISE)

    webhook = asyncio.create_task(post_webhook(client, body, "evt_cw"))
    await asyncio.wait_for(locked.wait(), 5)
    cancelling = asyncio.create_task(cancel(maker, order_id))
    await asyncio.sleep(0.2)
    assert not cancelling.done()  # waiting behind the webhook's row lock

    response = await webhook
    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert await cancelling is False
    row = await order_row(maker, order_id)
    assert (row["status"], row["provider_payment_id"]) == ("PAID", "pay_cw")
    assert row["paid_at"] is not None
    assert await stocks(maker, product_ids) == RESERVED
    assert len(published) == 1


async def test_payment_webhook_during_a_cancel_is_recorded_for_a_refund(
    client, maker, live_redis, published, caplog
) -> None:
    """The cancel has changed the order but not committed yet (cancel_order must commit with
    session.commit()). A webhook arriving in that moment must wait, then see CANCELLED."""
    cancel_order = import_cancel_order()
    order_id, product_ids = await make_reserved_order(maker, provider_order_id="order_webhook_vs_cancel")
    body = razorpay_body(provider_order_id="order_webhook_vs_cancel", payment_id="pay_wc", amount_paise=PAISE)
    reached, proceed = asyncio.Event(), asyncio.Event()

    async def cancel_but_stop_before_commit() -> bool:
        async with maker() as session:
            return await cancel_order(PauseBeforeCommit(session, reached, proceed), order_id)

    cancelling = asyncio.create_task(cancel_but_stop_before_commit())
    await asyncio.wait_for(reached.wait(), 5)
    with caplog.at_level(logging.ERROR, logger="app.payments"):
        webhook = asyncio.create_task(post_webhook(client, body, "evt_wc"))
        await asyncio.sleep(0.3)
        assert not webhook.done()  # waiting behind the cancel's row lock
        proceed.set()
        assert await cancelling is True
        response = await webhook

    assert response.status_code == 200
    row = await order_row(maker, order_id)
    assert (row["status"], row["provider_payment_id"]) == ("CANCELLED", "pay_wc")
    assert row["paid_at"] is not None
    assert await stocks(maker, product_ids) == BEFORE  # returned once
    assert published == []
    assert len(error_records(caplog)) == 1


async def test_cancel_and_webhook_racing_always_end_in_one_consistent_state(
    client, maker, live_redis, published
) -> None:
    """Best effort: ten rounds of a cancel and a payment webhook started together. Either the payment
    wins (PAID, stock stays reserved) or the cancel wins (CANCELLED, stock back once, payment recorded
    for a refund). Nothing else is allowed."""
    import_cancel_order()
    await warm_pool(maker)
    for i in range(10):
        order_id, product_ids = await make_reserved_order(maker, provider_order_id=f"order_round_{i}")
        body = razorpay_body(provider_order_id=f"order_round_{i}", payment_id=f"pay_round_{i}", amount_paise=PAISE)
        published.clear()

        cancelled, response = await asyncio.gather(cancel(maker, order_id), post_webhook(client, body, f"evt_round_{i}"))

        row = await order_row(maker, order_id)
        assert response.status_code == 200
        assert row["provider_payment_id"] == f"pay_round_{i}" and row["paid_at"] is not None
        if row["status"] == "PAID":
            assert cancelled is False
            assert await stocks(maker, product_ids) == RESERVED
            assert len(published) == 1
        else:
            assert (row["status"], cancelled) == ("CANCELLED", True)
            assert await stocks(maker, product_ids) == BEFORE
            assert published == []


# ------------------------------------------------------------------------------ D2


async def test_payment_for_a_cancelled_order_is_recorded_for_a_refund(
    client, maker, live_redis, published, caplog
) -> None:
    order_id, product_ids = await make_reserved_order(maker, status="CANCELLED", provider_order_id="order_late")
    body = razorpay_body(provider_order_id="order_late", payment_id="pay_late", amount_paise=PAISE)

    with caplog.at_level(logging.ERROR, logger="app.payments"):
        response = await post_webhook(client, body, "evt_late")

    assert response.status_code == 200, response.text
    assert response.json()["processed"] is False
    assert "refund" in response.json()["detail"].lower()
    row = await order_row(maker, order_id)
    assert (row["status"], row["provider_payment_id"]) == ("CANCELLED", "pay_late")
    assert row["paid_at"] is not None
    assert await refund_list(maker) == [order_id]
    assert await stocks(maker, product_ids) == RESERVED  # the webhook does not touch stock
    assert published == []
    (record,) = error_records(caplog)
    assert record.order_id == order_id
    assert record.provider_payment_id == "pay_late"
    assert record.amount_matches is True


async def test_payment_with_the_wrong_amount_for_a_cancelled_order_is_recorded_too(
    client, maker, live_redis, published, caplog
) -> None:
    """The money was taken whatever the amount, so it is recorded, and the log says it did not match."""
    order_id, _ = await make_reserved_order(maker, status="CANCELLED", provider_order_id="order_late_wrong")
    body = razorpay_body(provider_order_id="order_late_wrong", payment_id="pay_wrong", amount_paise=100)

    with caplog.at_level(logging.ERROR, logger="app.payments"):
        response = await post_webhook(client, body, "evt_late_wrong")

    assert response.status_code == 200, response.text
    row = await order_row(maker, order_id)
    assert (row["status"], row["provider_payment_id"]) == ("CANCELLED", "pay_wrong")
    assert row["paid_at"] is not None
    (record,) = error_records(caplog)
    assert record.amount_matches is False


async def test_second_payment_for_a_cancelled_order_does_not_overwrite_the_first(
    client, maker, live_redis, published, caplog
) -> None:
    order_id, _ = await make_reserved_order(maker, status="CANCELLED", provider_order_id="order_late_twice")
    first = razorpay_body(provider_order_id="order_late_twice", payment_id="pay_first", amount_paise=PAISE)
    second = razorpay_body(provider_order_id="order_late_twice", payment_id="pay_second", amount_paise=PAISE)

    with caplog.at_level(logging.ERROR, logger="app.payments"):
        await post_webhook(client, first, "evt_first")
        recorded = await order_row(maker, order_id)
        repeat = await post_webhook(client, first, "evt_first")  # the same event again
        other = await post_webhook(client, second, "evt_second")  # a different payment for the same order

    assert repeat.json()["detail"] == "Duplicate webhook ignored."
    assert other.status_code == 200
    assert await order_row(maker, order_id) == recorded  # still pay_first, same paid_at
    assert recorded["provider_payment_id"] == "pay_first"
    records = error_records(caplog)
    assert [r.provider_payment_id for r in records] == ["pay_first", "pay_second"]
    assert published == []


# ----------------------------------------------------------------------- the CHECK


@pytest.mark.parametrize(
    ("provider_payment_id", "paid_at"),
    [("pay_half", None), (None, datetime(2026, 1, 1, tzinfo=UTC))],
)
async def test_database_rejects_a_payment_id_without_paid_at_and_the_reverse(
    maker, provider_payment_id, paid_at
) -> None:
    from app.models.order import Order, OrderState
    from app.models.user import User, UserRole

    async with maker() as session:
        user = User(email="half-paid@example.com", hashed_password="x", role=UserRole.CUSTOMER)
        session.add(user)
        await session.flush()
        session.add(
            Order(
                user_id=user.id,
                status=OrderState.PENDING,
                payment_provider="razorpay",
                total_amount=Decimal("10.00"),
                shipping_address="12 Test Street",
                provider_payment_id=provider_payment_id,
                paid_at=paid_at,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
