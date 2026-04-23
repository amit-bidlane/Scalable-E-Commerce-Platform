import asyncio

from aio_pika.abc import AbstractIncomingMessage

from app.core.logging import configure_logging
from app.core.messaging import parse_message, rabbitmq_client
from app.schemas.notification import OrderPaidNotification

logger = configure_logging("app.worker")


async def handle_notification(message: AbstractIncomingMessage) -> None:
    async with message.process():
        payload = parse_message(message)
        notification = OrderPaidNotification.model_validate(payload)
        logger.info(
            "Sending email to %s for paid order %s | amount=%s | provider=%s | payment_id=%s",
            notification.user_email,
            notification.order_id,
            notification.total_amount,
            notification.payment_provider,
            notification.provider_payment_id,
        )


async def main() -> None:
    await rabbitmq_client.connect()
    await rabbitmq_client.consume(handle_notification)
    logger.info("Notification worker is consuming queue messages.")
    try:
        await asyncio.Future()
    finally:
        await rabbitmq_client.close()
        logger.info("Notification worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
