import json
import logging
from typing import Any

from aio_pika import DeliveryMode, Message, connect_robust
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractIncomingMessage, AbstractQueue

from app.core.config import settings

logger = logging.getLogger(__name__)


class RabbitMQClient:
    def __init__(self, url: str, queue_name: str) -> None:
        self._url = url
        self._queue_name = queue_name
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None

    async def connect(self) -> None:
        self._connection = await connect_robust(self._url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=10)
        self._queue = await self._channel.declare_queue(self._queue_name, durable=True)
        logger.info("RabbitMQ connection established.", extra={"queue": self._queue_name})

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        logger.info("RabbitMQ connection closed.", extra={"queue": self._queue_name})
        self._queue = None
        self._channel = None
        self._connection = None

    async def publish(self, payload: dict[str, Any]) -> None:
        if self._channel is None:
            logger.warning("RabbitMQ channel is not connected; notification message skipped.")
            return

        message = Message(
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
        )
        await self._channel.default_exchange.publish(message, routing_key=self._queue_name)
        logger.info("Published message to queue.", extra={"queue": self._queue_name, "payload": payload})

    async def consume(self, handler) -> None:
        if self._queue is None:
            raise RuntimeError("RabbitMQ queue is not connected.")
        await self._queue.consume(handler)


rabbitmq_client = RabbitMQClient(settings.rabbitmq_url, settings.notifications_queue)


def parse_message(message: AbstractIncomingMessage) -> dict[str, Any]:
    return json.loads(message.body.decode("utf-8"))
