from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.cache import (
    get_product_detail_cache_key,
    get_product_list_cache_key,
    invalidate_product_cache,
    redis_cache,
)
from app.core.config import settings
from app.core.rate_limiter import limiter, public_rate_limit
from app.core.rbac import admin_required
from app.db.session import get_db_session
from app.dependencies import get_current_user
from app.models.category import Category
from app.models.product import Product
from app.models.user import User
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate

router = APIRouter(prefix="/products", tags=["products"])


@router.get(
    "/search",
    response_model=list[ProductRead],
    summary="Search Products",
    description="Searches the product catalog with optional filters for category and price range.",
    response_description="Matching product list for the provided search criteria.",
)
@limiter.limit(public_rate_limit)
async def search_products(
    request: Request,
    q: str | None = Query(default=None, min_length=1),
    category_id: int | None = Query(default=None, ge=1),
    min_price: Decimal | None = Query(default=None, ge=0),
    max_price: Decimal | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductRead]:
    products = await _search_products(
        session=session,
        search=q,
        category_id=category_id,
        min_price=min_price,
        max_price=max_price,
    )
    return [_serialize_product(product) for product in products]


@router.get(
    "",
    response_model=list[ProductRead],
    summary="List Products",
    description="Returns product catalog entries, optionally filtered and served through the Redis caching layer.",
    response_description="Product list response, potentially served from cache.",
)
async def list_products(
    search: str | None = Query(default=None, min_length=1),
    category_id: int | None = Query(default=None, ge=1),
    min_price: Decimal | None = Query(default=None, ge=0),
    max_price: Decimal | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductRead]:
    cache_key = get_product_list_cache_key(
        search=search,
        category_id=category_id,
        min_price=float(min_price) if min_price is not None else None,
        max_price=float(max_price) if max_price is not None else None,
    )
    cached_products = await redis_cache.get_json(cache_key)
    if cached_products is not None:
        return [ProductRead.model_validate(product) for product in cached_products]

    products = await _search_products(
        session=session,
        search=search,
        category_id=category_id,
        min_price=min_price,
        max_price=max_price,
    )
    serialized_products = [_serialize_product(product).model_dump(mode="json") for product in products]
    await redis_cache.set_json(
        cache_key,
        serialized_products,
        ttl_seconds=settings.product_cache_ttl_seconds,
    )
    return [ProductRead.model_validate(product) for product in serialized_products]


@router.get(
    "/{product_id}",
    response_model=ProductRead,
    summary="Get Product by ID",
    description="Returns a single product detail record, using Redis caching for repeat lookups.",
    response_description="Single product detail payload.",
)
async def get_product(product_id: int, session: AsyncSession = Depends(get_db_session)) -> ProductRead:
    cache_key = get_product_detail_cache_key(product_id)
    cached_product = await redis_cache.get_json(cache_key)
    if cached_product is not None:
        return ProductRead.model_validate(cached_product)

    result = await session.execute(
        select(Product).options(selectinload(Product.categories)).where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    serialized_product = _serialize_product(product).model_dump(mode="json")
    await redis_cache.set_json(
        cache_key,
        serialized_product,
        ttl_seconds=settings.product_cache_ttl_seconds,
    )
    return ProductRead.model_validate(serialized_product)


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create Product",
    description="Admin-only endpoint to create a new catalog product and invalidate affected cache entries.",
    response_description="Created product payload.",
)
@admin_required
async def create_product(
    payload: ProductCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> Product:
    existing = await session.execute(
        select(Product).where(or_(Product.slug == payload.slug, Product.sku == payload.sku))
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Product slug or SKU already exists.")

    product = Product(
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        sku=payload.sku,
        price=payload.price,
        inventory_count=payload.inventory_count,
    )

    if payload.category_ids:
        categories = await _fetch_categories(payload.category_ids, session)
        product.categories = categories

    session.add(product)
    await session.commit()
    await session.refresh(product)
    await session.refresh(product, attribute_names=["categories"])
    serialized_product = _serialize_product(product)
    await invalidate_product_cache(redis_cache, product.id)
    await redis_cache.set_json(
        get_product_detail_cache_key(product.id),
        serialized_product.model_dump(mode="json"),
        ttl_seconds=settings.product_cache_ttl_seconds,
    )
    return serialized_product


@router.put(
    "/{product_id}",
    response_model=ProductRead,
    summary="Update Product",
    description="Admin-only endpoint to update a product and refresh both list and detail cache entries.",
    response_description="Updated product payload.",
)
@admin_required
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> Product:
    result = await session.execute(
        select(Product).options(selectinload(Product.categories)).where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    updates = payload.model_dump(exclude_unset=True, exclude={"category_ids"})
    for field, value in updates.items():
        setattr(product, field, value)

    if payload.category_ids is not None:
        product.categories = await _fetch_categories(payload.category_ids, session)

    await session.commit()
    await session.refresh(product)
    await session.refresh(product, attribute_names=["categories"])
    serialized_product = _serialize_product(product)
    await invalidate_product_cache(redis_cache, product.id)
    await redis_cache.set_json(
        get_product_detail_cache_key(product.id),
        serialized_product.model_dump(mode="json"),
        ttl_seconds=settings.product_cache_ttl_seconds,
    )
    return serialized_product


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Product",
    description="Admin-only endpoint to remove a product and invalidate any cached product responses.",
    response_description="Empty response when the product is successfully deleted.",
)
@admin_required
async def delete_product(
    product_id: int,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> Response:
    result = await session.execute(delete(Product).where(Product.id == product_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")

    await session.commit()
    await invalidate_product_cache(redis_cache, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _fetch_categories(category_ids: list[int], session: AsyncSession) -> list[Category]:
    result = await session.execute(select(Category).where(Category.id.in_(category_ids)))
    categories = list(result.scalars().all())
    if len(categories) != len(set(category_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more categories do not exist.")
    return categories


async def _search_products(
    *,
    session: AsyncSession,
    search: str | None = None,
    category_id: int | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
) -> list[Product]:
    query = select(Product).options(selectinload(Product.categories))

    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(Product.name.ilike(pattern), Product.description.ilike(pattern), Product.sku.ilike(pattern))
        )

    if category_id is not None:
        query = query.join(Product.categories).where(Category.id == category_id)

    if min_price is not None:
        query = query.where(Product.price >= min_price)

    if max_price is not None:
        query = query.where(Product.price <= max_price)

    query = query.order_by(Product.created_at.desc()).distinct()
    result = await session.execute(query)
    return list(result.scalars().unique().all())


def _serialize_product(product: Product) -> ProductRead:
    return ProductRead.model_validate(
        {
            "id": product.id,
            "name": product.name,
            "slug": product.slug,
            "description": product.description,
            "sku": product.sku,
            "price": product.price,
            "inventory_count": product.inventory_count,
            "category_ids": [category.id for category in product.categories],
        }
    )
