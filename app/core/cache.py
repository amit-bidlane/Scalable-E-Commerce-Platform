import json
import logging
from collections.abc import Iterable
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

PRODUCT_LIST_CACHE_KEY = "products:list"
PRODUCT_DETAIL_CACHE_KEY_PREFIX = "products:detail:"


class RedisCache:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Redis | None = None

    async def connect(self) -> None:
        self._client = Redis.from_url(self._redis_url, encoding="utf-8", decode_responses=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_json(self, key: str) -> Any | None:
        if self._client is None:
            return None

        try:
            raw = await self._client.get(key)
        except Exception:
            logger.exception("Redis get failed for key %s", key)
            return None

        if raw is None:
            return None

        return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        if self._client is None:
            return

        try:
            await self._client.set(key, json.dumps(value), ex=ttl_seconds)
        except Exception:
            logger.exception("Redis set failed for key %s", key)

    async def delete(self, *keys: str) -> None:
        if self._client is None or not keys:
            return

        try:
            await self._client.delete(*keys)
        except Exception:
            logger.exception("Redis delete failed for keys %s", keys)

    async def set_if_absent(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        if self._client is None:
            return False

        try:
            return bool(await self._client.set(key, value, ex=ttl_seconds, nx=True))
        except Exception:
            logger.exception("Redis conditional set failed for key %s", key)
            return False


def get_product_list_cache_key(
    *,
    search: str | None = None,
    category_id: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    return ":".join(
        [
            PRODUCT_LIST_CACHE_KEY,
            search or "",
            str(category_id or ""),
            str(min_price or ""),
            str(max_price or ""),
        ]
    )


def get_product_detail_cache_key(product_id: int) -> str:
    return f"{PRODUCT_DETAIL_CACHE_KEY_PREFIX}{product_id}"


async def invalidate_product_cache(cache: RedisCache, product_id: int | None = None) -> None:
    keys: list[str] = [PRODUCT_LIST_CACHE_KEY]
    if product_id is not None:
        keys.append(get_product_detail_cache_key(product_id))

    await cache.delete(*keys)

    if cache._client is None:
        return

    try:
        matching_keys: Iterable[str] = await cache._client.keys(f"{PRODUCT_LIST_CACHE_KEY}:*")
    except Exception:
        logger.exception("Redis key scan failed for product list invalidation")
        return

    if matching_keys:
        await cache.delete(*list(matching_keys))


redis_cache = RedisCache(settings.redis_url)
