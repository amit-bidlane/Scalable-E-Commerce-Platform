import pytest

from app.core.cache import redis_cache
from app.core.messaging import rabbitmq_client
from app.main import app


@pytest.fixture(autouse=True)
def stub_external_infra(monkeypatch):
    async def noop():
        return None

    monkeypatch.setattr(redis_cache, "connect", noop)
    monkeypatch.setattr(redis_cache, "close", noop)
    monkeypatch.setattr(rabbitmq_client, "connect", noop)
    monkeypatch.setattr(rabbitmq_client, "close", noop)
    yield
    app.dependency_overrides.clear()
