from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.main import app
from app.models.order import OrderState
from app.models.user import User, UserRole


def test_create_order_route_returns_provider_payload(monkeypatch) -> None:
    async def override_current_user() -> User:
        return User(
            id=5,
            email="buyer@example.com",
            hashed_password="hashed",
            role=UserRole.CUSTOMER,
        )

    async def fake_create_payment_order(payload, session, current_user):
        assert current_user.id == 5
        return {
            "order": {
                "id": 99,
                "status": OrderState.PENDING.value,
                "payment_provider": "razorpay",
                "provider_order_id": "order_123",
                "provider_payment_id": None,
                "total_amount": "499.99",
                "shipping_address": payload.shipping_address,
            },
            "provider": "razorpay",
            "provider_order_id": "order_123",
            "amount": "499.99",
            "currency": "INR",
            "client_secret": None,
            "publishable_key": None,
            "razorpay_key_id": "rzp_test_123",
        }

    monkeypatch.setattr("app.api.routes.orders.create_payment_order", fake_create_payment_order)
    app.dependency_overrides[get_current_user] = override_current_user

    client = TestClient(app)
    response = client.post(
        "/api/v1/orders",
        json={"shipping_address": "221B Baker Street", "provider": "razorpay"},
        headers={"Authorization": "Bearer fake-token"},
    )

    assert response.status_code == 201
    assert response.json()["provider_order_id"] == "order_123"

    app.dependency_overrides.clear()


def test_payment_webhook_route_returns_processing_status(monkeypatch) -> None:
    captured = {}

    async def fake_process_payment_webhook(provider, payload_bytes, headers, session):
        captured["provider"] = provider
        captured["payload"] = payload_bytes.decode("utf-8")
        captured["header_present"] = "x-razorpay-signature" in headers
        return True, "Order marked as paid."

    monkeypatch.setattr("app.api.routes.orders.process_payment_webhook", fake_process_payment_webhook)

    client = TestClient(app)
    response = client.post(
        "/api/v1/payments/webhooks/razorpay",
        content='{"event":"order.paid"}',
        headers={
            "x-razorpay-signature": "signature",
            "x-razorpay-event-id": "evt_123",
            "content-type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert captured["provider"] == "razorpay"
    assert captured["header_present"] is True
