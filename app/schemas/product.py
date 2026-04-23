from decimal import Decimal

from pydantic import BaseModel, Field


class ProductBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=280)
    description: str | None = None
    sku: str = Field(min_length=1, max_length=64)
    price: Decimal = Field(gt=0)
    inventory_count: int = Field(ge=0)


class ProductCreate(ProductBase):
    category_ids: list[int] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=280)
    description: str | None = None
    sku: str | None = Field(default=None, min_length=1, max_length=64)
    price: Decimal | None = Field(default=None, gt=0)
    inventory_count: int | None = Field(default=None, ge=0)
    category_ids: list[int] | None = None


class ProductRead(ProductBase):
    id: int
    category_ids: list[int] = Field(default_factory=list)

    model_config = {"from_attributes": True}
