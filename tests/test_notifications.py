from app.schemas.notification import OrderPaidNotification


def test_order_paid_notification_schema_serializes() -> None:
    payload = OrderPaidNotification(
        order_id=101,
        user_id=7,
        user_email="customer@example.com",
        total_amount="1499.99",
        payment_provider="razorpay",
        provider_payment_id="pay_abc123",
    )

    dumped = payload.model_dump(mode="json")

    assert dumped["order_id"] == 101
    assert dumped["user_email"] == "customer@example.com"
    assert dumped["payment_provider"] == "razorpay"
