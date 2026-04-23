import hashlib
import hmac
import json

from app.services.payments import _build_idempotency_key, _verify_razorpay_webhook


def test_build_idempotency_key_prefers_event_id() -> None:
    assert _build_idempotency_key("stripe", {"id": "evt_123"}) == "payments:webhook:stripe:evt_123"
    assert _build_idempotency_key("razorpay", {"_event_id": "evt_456"}) == "payments:webhook:razorpay:evt_456"


def test_verify_razorpay_webhook_uses_raw_body_signature(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "secret")
    payload = {"event": "order.paid", "payload": {"payment": {"entity": {"id": "pay_123"}}}}
    payload_bytes = json.dumps(payload).encode("utf-8")
    signature = hmac.new(b"secret", payload_bytes, hashlib.sha256).hexdigest()

    event = _verify_razorpay_webhook(
        payload_bytes,
        {"x-razorpay-signature": signature, "x-razorpay-event-id": "evt_789"},
    )

    assert event["_event_id"] == "evt_789"
    assert event["event"] == "order.paid"
