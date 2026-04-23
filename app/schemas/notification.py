from decimal import Decimal

from pydantic import BaseModel, EmailStr


class OrderPaidNotification(BaseModel):
    order_id: int
    user_id: int
    user_email: EmailStr
    total_amount: Decimal
    payment_provider: str
    provider_payment_id: str
