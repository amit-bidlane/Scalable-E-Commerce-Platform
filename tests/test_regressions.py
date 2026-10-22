"""Regression tests for known bugs: privilege escalation at registration, refresh-token
reuse, payment webhook handling, and the initial migration.

Each test states the correct behaviour, so it fails on code that still has the bug.

Needs a real PostgreSQL and Redis. Settings (defaults in brackets):
TEST_PG_HOST [localhost], TEST_PG_PORT [5432], TEST_PG_USER [postgres],
TEST_PG_PASSWORD [postgres], TEST_PG_DB [ecommerce_test],
TEST_REDIS_URL [redis://localhost:6379/5].

WARNING: these tests drop and recreate the public schema in TEST_PG_DB. Use a test
database only.

The tests create their own schema with `Base.metadata.create_all`, so they do not depend
on Alembic, except `test_migrations_apply_on_empty_database`, which tests Alembic on purpose.
"""

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PG_HOST = os.getenv("TEST_PG_HOST", "localhost")
PG_PORT = os.getenv("TEST_PG_PORT", "5432")
PG_USER = os.getenv("TEST_PG_USER", "postgres")
PG_PASSWORD = os.getenv("TEST_PG_PASSWORD", "postgres")
PG_DB = os.getenv("TEST_PG_DB", "ecommerce_test")
REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/5")
REPO_ROOT = Path(__file__).resolve().parents[1]

PASSWORD = "Str0ngPassw0rd!"
WEBHOOK_SECRET = "whsec_regression_test"


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
async def engine():
    import app.models  # noqa: F401  (registers all tables on Base.metadata)
    from app.db.base import Base

    eng = create_async_engine(
        f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"
    )
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def client(engine, maker):
    from app.db.session import get_db_session
    from app.main import app

    async def override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
async def live_redis():
    """Connect the app's cache object to a real Redis (the default test setup stubs it)."""
    from app.core.cache import redis_cache

    redis = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    await redis.flushdb()
    previous = redis_cache._client
    redis_cache._client = redis
    yield redis
    redis_cache._client = previous
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
def no_redis(monkeypatch):
    """Simulate a Redis outage: the cache object has no connection."""
    from app.core.cache import redis_cache

    monkeypatch.setattr(redis_cache, "_client", None)


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "razorpay_webhook_secret", WEBHOOK_SECRET)


# -------------------------------------------------------------------------- helpers


async def register(c, email: str, **extra):
    return await c.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, **extra}
    )


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def make_order(maker, *, total: str, provider_order_id: str) -> int:
    from app.models.order import Order, OrderState
    from app.models.user import User, UserRole

    async with maker() as session:
        user = User(email=f"buyer-{provider_order_id}@example.com", hashed_password="x", role=UserRole.CUSTOMER)
        session.add(user)
        await session.flush()
        order = Order(
            user_id=user.id,
            status=OrderState.PENDING,
            payment_provider="razorpay",
            total_amount=Decimal(total),
            shipping_address="12 Test Street",
            provider_order_id=provider_order_id,
        )
        session.add(order)
        await session.commit()
        return order.id


async def order_status(maker, order_id: int) -> str:
    from app.models.order import Order

    async with maker() as session:
        status = (await session.execute(select(Order.status).where(Order.id == order_id))).scalar_one()
        return status.value


def razorpay_body(*, provider_order_id: str, payment_id: str, amount_paise: int) -> bytes:
    return json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": provider_order_id,
                        "amount": amount_paise,
                        "currency": "INR",
                    }
                }
            },
        }
    ).encode()


async def post_webhook(c, body: bytes, event_id: str):
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return await c.post(
        "/api/v1/payments/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": event_id,
            "content-type": "application/json",
        },
    )


# ------------------------------------------------------------------------- security


@pytest.mark.asyncio
async def test_public_registration_cannot_create_an_admin(client) -> None:
    """A caller must not be able to choose their own role."""
    response = await register(client, "evil@example.com", role="admin")
    assert response.status_code in (201, 422), response.text
    if response.status_code == 422:
        return

    tokens = response.json()
    me = await client.get("/api/v1/auth/me", headers=bearer(tokens))
    assert me.json()["role"] == "customer"

    product = {"name": "Hacked", "slug": "hacked", "sku": "H-1", "price": "1.00", "inventory_count": 1}
    created = await client.post("/api/v1/products", headers=bearer(tokens), json=product)
    assert created.status_code == 403


@pytest.mark.asyncio
async def test_refresh_token_cannot_be_reused(client, live_redis) -> None:
    tokens = (await register(client, "refresh@example.com")).json()

    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    second = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert second.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_works_only_once_under_concurrency(client, live_redis) -> None:
    tokens = (await register(client, "race@example.com")).json()

    responses = await asyncio.gather(
        *[
            client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
            for _ in range(5)
        ]
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 401, 401, 401, 401], statuses


# ------------------------------------------------------------------------- payments


@pytest.mark.asyncio
async def test_webhook_is_never_silently_dropped_when_redis_is_down(client, maker, no_redis) -> None:
    """With Redis down, either ask the provider to retry (5xx) or still process the payment.
    The one wrong answer is HTTP 200 while the order stays PENDING."""
    order_id = await make_order(maker, total="500.00", provider_order_id="order_down")
    body = razorpay_body(provider_order_id="order_down", payment_id="pay_down", amount_paise=50000)

    response = await post_webhook(client, body, "evt_down")

    assert response.status_code >= 500 or await order_status(maker, order_id) == "PAID", (
        response.status_code,
        response.text,
    )


@pytest.mark.asyncio
async def test_webhook_with_wrong_amount_does_not_mark_order_paid(client, maker, live_redis) -> None:
    order_id = await make_order(maker, total="500.00", provider_order_id="order_wrong_amount")
    body = razorpay_body(provider_order_id="order_wrong_amount", payment_id="pay_1", amount_paise=100)

    await post_webhook(client, body, "evt_wrong_amount")

    assert await order_status(maker, order_id) != "PAID"


@pytest.mark.asyncio
async def test_webhook_with_matching_amount_marks_order_paid(client, maker, live_redis) -> None:
    """Guard: the amount check must not break the normal path. INR 500.00 = 50000 paise."""
    order_id = await make_order(maker, total="500.00", provider_order_id="order_ok")
    body = razorpay_body(provider_order_id="order_ok", payment_id="pay_ok", amount_paise=50000)

    response = await post_webhook(client, body, "evt_ok")

    assert response.status_code == 200, response.text
    assert await order_status(maker, order_id) == "PAID"


@pytest.mark.asyncio
async def test_webhook_with_bad_signature_is_rejected(client, maker, live_redis) -> None:
    """Guard: this already works. Keep it working."""
    order_id = await make_order(maker, total="500.00", provider_order_id="order_sig")
    body = razorpay_body(provider_order_id="order_sig", payment_id="pay_sig", amount_paise=50000)

    response = await client.post(
        "/api/v1/payments/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": "0" * 64, "x-razorpay-event-id": "evt_sig"},
    )

    assert response.status_code == 400
    assert await order_status(maker, order_id) == "PENDING"


# ------------------------------------------------------------------------ migrations


def test_migrations_apply_on_empty_database() -> None:
    import psycopg

    name = "ecommerce_migration_regression"
    admin = psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname="postgres", autocommit=True
    )
    try:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()

    env = {
        **os.environ,
        "POSTGRES_SERVER": PG_HOST,
        "POSTGRES_PORT": PG_PORT,
        "POSTGRES_USER": PG_USER,
        "POSTGRES_PASSWORD": PG_PASSWORD,
        "POSTGRES_DB": name,
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
