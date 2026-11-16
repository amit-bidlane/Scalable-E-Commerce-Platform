from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.dependencies import get_current_user
from app.models.cart import Cart, CartItem
from app.models.product import Product
from app.models.user import User
from app.schemas.cart import MAX_LINE_QUANTITY, CartItemCreate, CartItemUpdate, CartRead

router = APIRouter(prefix="/cart", tags=["cart"])


async def _get_product(session: AsyncSession, product_id: int):
    product = (
        await session.execute(select(Product.price, Product.inventory_count).where(Product.id == product_id))
    ).one_or_none()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found.")
    return product


def _users_line(user_id: int, product_id: int):
    cart_id = select(Cart.id).where(Cart.user_id == user_id).scalar_subquery()
    return CartItem.cart_id == cart_id, CartItem.product_id == product_id


async def _read_cart(session: AsyncSession, user_id: int) -> CartRead:
    """The cart with the product's CURRENT price. The unit_price stored on the line is never read by the
    cart routes; POST /orders still reads it until Task 3 re-prices from the product."""
    rows = (
        await session.execute(
            select(CartItem.product_id, CartItem.quantity, Product.price)
            .join(Cart, Cart.id == CartItem.cart_id)
            .join(Product, Product.id == CartItem.product_id)
            .where(Cart.user_id == user_id)
            .order_by(CartItem.id)
        )
    ).all()
    items = [
        {"product_id": product_id, "quantity": quantity, "unit_price": price, "line_total": price * quantity}
        for product_id, quantity, price in rows
    ]
    return CartRead(items=items, total=sum((item["line_total"] for item in items), Decimal("0.00")))


async def _commit_and_read_cart(session: AsyncSession, user_id: int) -> CartRead:
    cart = await _read_cart(session, user_id)
    await session.commit()
    return cart


@router.get(
    "",
    response_model=CartRead,
    summary="Get My Cart",
    description="Returns the authenticated user's cart with current product prices and server-computed totals.",
    response_description="Cart lines, line totals and the cart total.",
)
async def get_cart(
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> CartRead:
    return await _read_cart(session, current_user.id)


@router.post(
    "/items",
    response_model=CartRead,
    summary="Add To Cart",
    description=(
        "Adds the quantity to the product's cart line, creating the cart and the line when needed. "
        "A line holds at most 99 units. Stock is checked here but not reserved."
    ),
    response_description="The updated cart.",
)
async def add_item(
    payload: CartItemCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> CartRead:
    product = await _get_product(session, payload.product_id)

    # One statement creates the cart or finds the existing one. The no-op update is what lets RETURNING
    # give back the id of a row that already exists.
    new_cart = insert(Cart).values(user_id=current_user.id)
    cart_id = (
        await session.execute(
            new_cart.on_conflict_do_update(index_elements=["user_id"], set_={"updated_at": func.now()}).returning(
                Cart.id
            )
        )
    ).scalar_one()

    # One statement adds the line or adds to it. The WHERE keeps the line at 99 or less even when
    # several requests arrive together; when it is false no row is touched and nothing is returned.
    new_line = insert(CartItem).values(
        cart_id=cart_id, product_id=payload.product_id, quantity=payload.quantity, unit_price=product.price
    )
    quantity = (
        await session.execute(
            new_line.on_conflict_do_update(
                index_elements=["cart_id", "product_id"],
                set_={
                    "quantity": CartItem.quantity + new_line.excluded.quantity,
                    "unit_price": new_line.excluded.unit_price,
                    "updated_at": func.now(),
                },
                where=CartItem.quantity + new_line.excluded.quantity <= MAX_LINE_QUANTITY,
            ).returning(CartItem.quantity)
        )
    ).scalar_one_or_none()
    if quantity is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"A cart line can hold at most {MAX_LINE_QUANTITY} units.",
        )
    if quantity > product.inventory_count:
        # Nothing is committed, so the change above is rolled back when the session closes.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not enough stock for this quantity.")

    return await _commit_and_read_cart(session, current_user.id)


@router.put(
    "/items/{product_id}",
    response_model=CartRead,
    summary="Set Cart Line Quantity",
    description="Sets the quantity of a product that is already in the cart. Stock is checked but not reserved.",
    response_description="The updated cart.",
)
async def set_item_quantity(
    product_id: int,
    payload: CartItemUpdate,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> CartRead:
    product = await _get_product(session, product_id)
    if payload.quantity > product.inventory_count:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not enough stock for this quantity.")

    updated = (
        await session.execute(
            update(CartItem)
            .where(*_users_line(current_user.id, product_id))
            .values(quantity=payload.quantity, unit_price=product.price)
            .returning(CartItem.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That product is not in your cart.")

    return await _commit_and_read_cart(session, current_user.id)


@router.delete(
    "/items/{product_id}",
    response_model=CartRead,
    summary="Remove From Cart",
    description="Removes the product's line from the cart. Removing a line that is not there is not an error.",
    response_description="The updated cart.",
)
async def remove_item(
    product_id: int,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> CartRead:
    await session.execute(
        delete(CartItem)
        .where(*_users_line(current_user.id, product_id))
        .execution_options(synchronize_session=False)
    )
    return await _commit_and_read_cart(session, current_user.id)
