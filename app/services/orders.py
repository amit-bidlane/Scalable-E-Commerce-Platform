from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem, OrderState
from app.models.product import Product


async def cancel_order(session: AsyncSession, order_id: int) -> bool:
    """Cancel a PENDING order and give back the stock its items reserved.

    Returns True when the order was PENDING and is now CANCELLED. Returns False, and commits nothing,
    when the order does not exist or is not PENDING.

    The stock it returns is the stock that checkout reserves from Task 3 on. Orders created before that
    hold no reservation, so this must not be run on them. No route calls it yet.

    Locking order, for all code: the order row first, then the product rows in id order.
    """
    # One conditional statement: of two calls for the same order, only one gets a row back.
    cancelled = (
        await session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderState.PENDING)
            .values(status=OrderState.CANCELLED)
            .returning(Order.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if cancelled is None:
        return False

    # Lock the product rows in id order, so two cancels that share products cannot wait on each other.
    order_products = select(OrderItem.product_id).where(OrderItem.order_id == order_id)
    await session.execute(
        select(Product.id).where(Product.id.in_(order_products)).order_by(Product.id).with_for_update()
    )

    await session.execute(
        update(Product)
        .where(Product.id == OrderItem.product_id, OrderItem.order_id == order_id)
        .values(inventory_count=Product.inventory_count + OrderItem.quantity)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return True
