from decimal import Decimal

from pydantic import BaseModel, Field

MAX_LINE_QUANTITY = 99


class CartItemCreate(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=MAX_LINE_QUANTITY)


class CartItemUpdate(BaseModel):
    quantity: int = Field(ge=1, le=MAX_LINE_QUANTITY)


class CartItemRead(BaseModel):
    product_id: int
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class CartRead(BaseModel):
    items: list[CartItemRead]
    total: Decimal
