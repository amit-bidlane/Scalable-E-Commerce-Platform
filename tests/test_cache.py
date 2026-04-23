import asyncio

from app.core.cache import (
    PRODUCT_LIST_CACHE_KEY,
    RedisCache,
    get_product_detail_cache_key,
    get_product_list_cache_key,
    invalidate_product_cache,
)


class FakeRedisClient:
    def __init__(self) -> None:
        self.deleted_keys: list[str] = []

    async def delete(self, *keys: str) -> None:
        self.deleted_keys.extend(keys)

    async def keys(self, pattern: str) -> list[str]:
        assert pattern == f"{PRODUCT_LIST_CACHE_KEY}:*"
        return [f"{PRODUCT_LIST_CACHE_KEY}:search", f"{PRODUCT_LIST_CACHE_KEY}:category"]


def test_product_cache_key_builders() -> None:
    assert get_product_detail_cache_key(42) == "products:detail:42"
    assert get_product_list_cache_key(search="phone", category_id=3, min_price=100.0, max_price=500.0) == (
        "products:list:phone:3:100.0:500.0"
    )


def test_invalidate_product_cache_removes_list_and_detail_keys() -> None:
    cache = RedisCache("redis://example")
    fake_client = FakeRedisClient()
    cache._client = fake_client  # type: ignore[assignment]

    asyncio.run(invalidate_product_cache(cache, 10))

    assert PRODUCT_LIST_CACHE_KEY in fake_client.deleted_keys
    assert "products:detail:10" in fake_client.deleted_keys
    assert f"{PRODUCT_LIST_CACHE_KEY}:search" in fake_client.deleted_keys
    assert f"{PRODUCT_LIST_CACHE_KEY}:category" in fake_client.deleted_keys
