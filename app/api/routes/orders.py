from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.order import OrderCreateRequest, PaymentOrderCreateResponse, PaymentWebhookResponse
from app.services.payments import create_payment_order, process_payment_webhook

router = APIRouter(tags=["payments"])


@router.post(
    "/orders",
    response_model=PaymentOrderCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Checkout Order",
    description="Creates a local order from the authenticated user's cart and provisions the provider-side payment order or intent.",
    response_description="Local order data plus payment provider checkout details.",
)
async def create_order(
    payload: OrderCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> PaymentOrderCreateResponse:
    return await create_payment_order(payload=payload, session=session, current_user=current_user)


@router.post(
    "/payments/webhooks/{provider}",
    response_model=PaymentWebhookResponse,
    summary="Receive Payment Webhook",
    description="Verifies the provider signature, applies idempotency protection, and marks the order as PAID exactly once.",
    response_description="Acknowledgement describing whether the webhook was processed or ignored.",
)
async def payment_webhook(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> PaymentWebhookResponse:
    payload = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    if provider == "razorpay":
        event_id = request.headers.get("x-razorpay-event-id")
        if event_id:
            headers["x-razorpay-event-id"] = event_id

    processed, detail = await process_payment_webhook(
        provider=provider,
        payload_bytes=payload,
        headers=headers,
        session=session,
    )
    return PaymentWebhookResponse(processed=processed, detail=detail)
