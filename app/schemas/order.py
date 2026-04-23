from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.order import OrderState


class OrderCreateRequest(BaseModel):
    shipping_address: str = Field(min_length=5, max_length=500)
    provider: str | None = None


class OrderRead(BaseModel):
    id: int
    status: OrderState
    payment_provider: str
    provider_order_id: str | None = None
    provider_payment_id: str | None = None
    total_amount: Decimal
    shipping_address: str

    model_config = {"from_attributes": True}


class PaymentOrderCreateResponse(BaseModel):
    order: OrderRead
    provider: str
    provider_order_id: str
    amount: Decimal
    currency: str
    client_secret: str | None = None
    publishable_key: str | None = None
    razorpay_key_id: str | None = None


class PaymentWebhookResponse(BaseModel):
    received: bool = True
    processed: bool
    detail: str
