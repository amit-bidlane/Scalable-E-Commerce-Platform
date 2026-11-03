import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import razorpay
import stripe
from fastapi import HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.cache import redis_cache
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.messaging import rabbitmq_client
from app.models.cart import Cart
from app.models.order import Order, OrderItem, OrderState
from app.models.user import User
from app.schemas.notification import OrderPaidNotification
from app.schemas.order import OrderCreateRequest, OrderRead, PaymentOrderCreateResponse

logger = configure_logging("app.payments")


def _payment_subunits(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1")))


async def create_payment_order(
    *,
    payload: OrderCreateRequest,
    session: AsyncSession,
    current_user: User,
) -> PaymentOrderCreateResponse:
    provider = (payload.provider or settings.payment_provider).lower()
    cart = await _get_cart_for_checkout(session, current_user.id)
    total_amount = sum((item.unit_price * item.quantity for item in cart.items), start=Decimal("0.00"))

    order = Order(
        user_id=current_user.id,
        status=OrderState.PENDING,
        payment_provider=provider,
        total_amount=total_amount,
        shipping_address=payload.shipping_address,
        items=[
            OrderItem(
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in cart.items
        ],
    )
    session.add(order)
    await session.flush()

    provider_response = _create_provider_order(provider=provider, order=order)
    order.provider_order_id = provider_response["provider_order_id"]

    await session.commit()
    await session.refresh(order)

    return PaymentOrderCreateResponse(
        order=OrderRead.model_validate(order),
        provider=provider,
        provider_order_id=provider_response["provider_order_id"],
        amount=order.total_amount,
        currency=settings.payment_currency,
        client_secret=provider_response.get("client_secret"),
        publishable_key=provider_response.get("publishable_key"),
        razorpay_key_id=provider_response.get("razorpay_key_id"),
    )


async def process_payment_webhook(
    *,
    provider: str,
    payload_bytes: bytes,
    headers: dict[str, str],
    session: AsyncSession,
) -> tuple[bool, str]:
    normalized_provider = provider.lower()
    event = _verify_and_parse_webhook(normalized_provider, payload_bytes, headers)
    if normalized_provider == "stripe" and event.get("type") != "payment_intent.succeeded":
        return False, "Unhandled Stripe event."
    if normalized_provider == "razorpay" and event.get("event") not in {"order.paid", "payment.captured"}:
        return False, "Unhandled Razorpay event."

    idempotency_key = _build_idempotency_key(normalized_provider, event)

    try:
        acquired = await redis_cache.set_if_absent(
            idempotency_key,
            "processed",
            ttl_seconds=settings.payment_webhook_idempotency_ttl_seconds,
        )
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook cannot be processed right now. Please retry.",
        ) from exc
    if not acquired:
        return False, "Duplicate webhook ignored."

    try:
        order, provider_payment_id = await _resolve_order_from_event(normalized_provider, event, session)
        if order.status == OrderState.PAID:
            return False, "Order already marked as paid."
        if order.status != OrderState.PENDING:
            return False, "Order is not waiting for payment."

        if not _payment_matches_order(normalized_provider, event, order):
            logger.warning(
                "Payment amount or currency does not match the order.",
                extra={"order_id": order.id, "payment_provider": normalized_provider},
            )
            return False, "Payment amount or currency does not match the order."

        order.status = OrderState.PAID
        order.provider_payment_id = provider_payment_id
        order.paid_at = datetime.now(UTC)
        await session.commit()
        order_with_user = await _fetch_order_with_user(session, order.id)
        await _publish_order_paid_notification(order_with_user)
        logger.info(
            "Order payment confirmed.",
            extra={
                "order_id": order.id,
                "payment_provider": normalized_provider,
                "provider_payment_id": provider_payment_id,
                "status": OrderState.PAID.value,
            },
        )
        return True, "Order marked as paid."
    except Exception:
        await redis_cache.delete(idempotency_key)
        raise


def _create_provider_order(*, provider: str, order: Order) -> dict[str, str]:
    if provider == "razorpay":
        return _create_razorpay_order(order)
    if provider == "stripe":
        return _create_stripe_payment_intent(order)

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported payment provider.")


def _create_razorpay_order(order: Order) -> dict[str, str]:
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Razorpay is not configured.")

    client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    response = client.order.create(
        {
            "amount": _payment_subunits(order.total_amount),
            "currency": settings.payment_currency,
            "receipt": f"order-{order.id}",
            "notes": {"local_order_id": str(order.id)},
        }
    )
    return {
        "provider_order_id": response["id"],
        "razorpay_key_id": settings.razorpay_key_id,
    }


def _create_stripe_payment_intent(order: Order) -> dict[str, str]:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe is not configured.")

    stripe.api_key = settings.stripe_secret_key
    intent = stripe.PaymentIntent.create(
        amount=_payment_subunits(order.total_amount),
        currency=settings.payment_currency.lower(),
        metadata={"order_id": str(order.id)},
    )
    return {
        "provider_order_id": intent["id"],
        "client_secret": intent["client_secret"],
        "publishable_key": settings.stripe_publishable_key,
    }


def _verify_and_parse_webhook(provider: str, payload_bytes: bytes, headers: dict[str, str]) -> dict[str, Any]:
    if provider == "stripe":
        return _verify_stripe_webhook(payload_bytes, headers)
    if provider == "razorpay":
        return _verify_razorpay_webhook(payload_bytes, headers)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported payment provider.")


def _verify_stripe_webhook(payload_bytes: bytes, headers: dict[str, str]) -> dict[str, Any]:
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stripe is not configured.")

    stripe.api_key = settings.stripe_secret_key
    signature = headers.get("stripe-signature")
    if not signature:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Stripe signature.")

    try:
        event = stripe.Webhook.construct_event(payload_bytes, signature, settings.stripe_webhook_secret)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe webhook signature.") from exc

    return dict(event)


def _verify_razorpay_webhook(payload_bytes: bytes, headers: dict[str, str]) -> dict[str, Any]:
    if not settings.razorpay_webhook_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Razorpay is not configured.")

    signature = headers.get("x-razorpay-signature")
    if not signature:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Razorpay signature.")

    expected = hmac.new(
        settings.razorpay_webhook_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Razorpay webhook signature.")

    event = json.loads(payload_bytes.decode("utf-8"))
    event["_event_id"] = headers.get("x-razorpay-event-id")
    return event


def _build_idempotency_key(provider: str, event: dict[str, Any]) -> str:
    if provider == "stripe":
        event_id = event.get("id")
    else:
        event_id = event.get("_event_id") or event.get("event_id")

    if not event_id:
        payment_id = _extract_payment_id(provider, event)
        if not payment_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to determine event idempotency.")
        event_id = payment_id

    return f"payments:webhook:{provider}:{event_id}"


async def _resolve_order_from_event(
    provider: str,
    event: dict[str, Any],
    session: AsyncSession,
) -> tuple[Order, str]:
    if provider == "stripe":
        intent = event["data"]["object"]
        local_order_id = intent.get("metadata", {}).get("order_id")
        if not local_order_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Stripe metadata is missing order_id.")
        provider_payment_id = intent["id"]
        order = await _fetch_order_for_update(session, order_id=int(local_order_id))
        return order, provider_payment_id

    payload = event.get("payload", {})
    order_entity = payload.get("order", {}).get("entity", {})
    payment_entity = payload.get("payment", {}).get("entity", {})
    provider_order_id = order_entity.get("id") or payment_entity.get("order_id")
    provider_payment_id = payment_entity.get("id")
    if not provider_order_id or not provider_payment_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload is missing payment linkage.")

    result = await session.execute(
        select(Order).where(Order.provider_order_id == provider_order_id).with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found for payment event.")
    return order, provider_payment_id


async def _fetch_order_for_update(session: AsyncSession, *, order_id: int) -> Order:
    result = await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return order


async def _fetch_order_with_user(session: AsyncSession, order_id: int) -> Order:
    result = await session.execute(
        select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return order


async def _get_cart_for_checkout(session: AsyncSession, user_id: int) -> Cart:
    result = await session.execute(
        select(Cart)
        .options(selectinload(Cart.items))
        .where(Cart.user_id == user_id)
    )
    cart = result.scalar_one_or_none()
    if cart is None or not cart.items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cart is empty.")
    return cart


def _payment_matches_order(provider: str, event: dict[str, Any], order: Order) -> bool:
    if provider == "stripe":
        entity = event.get("data", {}).get("object", {})
    else:
        entity = event.get("payload", {}).get("payment", {}).get("entity", {})

    amount = entity.get("amount")
    currency = entity.get("currency")
    if amount is None or not currency:
        return False

    try:
        provider_amount = Decimal(str(amount))
    except (InvalidOperation, TypeError):
        return False

    return (
        provider_amount == _payment_subunits(order.total_amount)
        and str(currency).upper() == settings.payment_currency.upper()
    )


def _extract_payment_id(provider: str, event: dict[str, Any]) -> str | None:
    if provider == "stripe":
        return event.get("data", {}).get("object", {}).get("id")
    payload = event.get("payload", {})
    return payload.get("payment", {}).get("entity", {}).get("id")


async def _publish_order_paid_notification(order: Order) -> None:
    if order.user is None or order.provider_payment_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Order notification payload is incomplete.")

    notification = OrderPaidNotification(
        order_id=order.id,
        user_id=order.user_id,
        user_email=order.user.email,
        total_amount=order.total_amount,
        payment_provider=order.payment_provider,
        provider_payment_id=order.provider_payment_id,
    )
    await rabbitmq_client.publish(notification.model_dump(mode="json"))
