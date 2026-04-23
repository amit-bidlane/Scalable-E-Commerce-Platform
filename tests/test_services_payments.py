from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.order import Order, OrderState
from app.services import payments as payment_service


class ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, order):
        self.order = order
        self.committed = False

    async def execute(self, query):
        return ScalarResult(self.order)

    async def commit(self):
        self.committed = True


class FakeRedisCache:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.deleted_keys = []

    async def set_if_absent(self, key, value, ttl_seconds=None):
        return self.acquired

    async def delete(self, *keys):
        self.deleted_keys.extend(keys)


@pytest.mark.asyncio
async def test_process_payment_webhook_ignores_duplicate_event(monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "_verify_and_parse_webhook", lambda provider, payload, headers: {"id": "evt_1", "type": "payment_intent.succeeded"})
    monkeypatch.setattr(payment_service, "redis_cache", FakeRedisCache(acquired=False))

    processed, detail = await payment_service.process_payment_webhook(
        provider="stripe",
        payload_bytes=b"{}",
        headers={"stripe-signature": "sig"},
        session=FakeSession(order=None),
    )

    assert processed is False
    assert detail == "Duplicate webhook ignored."


@pytest.mark.asyncio
async def test_process_payment_webhook_marks_order_paid_and_publishes(monkeypatch) -> None:
    order = Order(
        id=15,
        user_id=8,
        status=OrderState.PENDING,
        payment_provider="stripe",
        total_amount=Decimal("999.99"),
        shipping_address="42 Wallaby Way",
    )
    order.user = type("UserStub", (), {"email": "buyer@example.com"})()

    async def fake_resolve(provider, event, session):
        return order, "pi_123"

    async def fake_fetch_with_user(session, order_id):
        return order

    published = {}

    async def fake_publish(order_obj):
        published["order_id"] = order_obj.id

    monkeypatch.setattr(payment_service, "_verify_and_parse_webhook", lambda provider, payload, headers: {"id": "evt_2", "type": "payment_intent.succeeded"})
    monkeypatch.setattr(payment_service, "redis_cache", FakeRedisCache(acquired=True))
    monkeypatch.setattr(payment_service, "_resolve_order_from_event", fake_resolve)
    monkeypatch.setattr(payment_service, "_fetch_order_with_user", fake_fetch_with_user)
    monkeypatch.setattr(payment_service, "_publish_order_paid_notification", fake_publish)

    session = FakeSession(order)
    processed, detail = await payment_service.process_payment_webhook(
        provider="stripe",
        payload_bytes=b"{}",
        headers={"stripe-signature": "sig"},
        session=session,
    )

    assert processed is True
    assert detail == "Order marked as paid."
    assert order.status == OrderState.PAID
    assert order.provider_payment_id == "pi_123"
    assert published["order_id"] == 15
    assert session.committed is True


def test_extract_payment_id_for_razorpay() -> None:
    payment_id = payment_service._extract_payment_id(
        "razorpay",
        {"payload": {"payment": {"entity": {"id": "pay_test_123"}}}},
    )

    assert payment_id == "pay_test_123"


def test_create_provider_order_rejects_unknown_provider() -> None:
    order = Order(
        id=1,
        user_id=1,
        status=OrderState.PENDING,
        payment_provider="unknown",
        total_amount=Decimal("10.00"),
        shipping_address="Unknown street",
    )

    with pytest.raises(HTTPException) as exc:
        payment_service._create_provider_order(provider="unknown", order=order)

    assert exc.value.status_code == 400
