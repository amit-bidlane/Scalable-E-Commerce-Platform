import os
from collections.abc import Mapping
from urllib.parse import urlsplit


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def resolve_test_env(env: Mapping[str, str]) -> dict[str, str]:
    """Build the POSTGRES_* and REDIS_URL values from TEST_*, so the app itself uses the test databases.

    Outside CI these are refused: the default ports (5432, 6379), which belong to the dev containers;
    a Postgres or Redis host that is not local; and a database name without "test" in it, because
    the regression tests run DROP SCHEMA on that database.
    """
    expected = {
        "POSTGRES_SERVER": env.get("TEST_PG_HOST", "localhost"),
        "POSTGRES_PORT": env.get("TEST_PG_PORT", "5432"),
        "POSTGRES_USER": env.get("TEST_PG_USER", "postgres"),
        "POSTGRES_PASSWORD": env.get("TEST_PG_PASSWORD", "postgres"),
        "POSTGRES_DB": env.get("TEST_PG_DB", "ecommerce_test"),
        "REDIS_URL": env.get("TEST_REDIS_URL", "redis://localhost:6379/5"),
    }
    if not env.get("CI"):
        problems = []
        redis = urlsplit(expected["REDIS_URL"])
        if int(expected["POSTGRES_PORT"]) == 5432:
            problems.append("TEST_PG_PORT is the default Postgres port 5432")
        if (redis.port or 6379) == 6379:
            problems.append("TEST_REDIS_URL uses the default Redis port 6379")
        if expected["POSTGRES_SERVER"] not in LOCAL_HOSTS:
            problems.append("TEST_PG_HOST is not a local host")
        if redis.hostname not in LOCAL_HOSTS:
            problems.append("TEST_REDIS_URL host is not a local host")
        if "test" not in expected["POSTGRES_DB"].lower():
            problems.append('TEST_PG_DB does not contain "test"')
        if problems:
            raise RuntimeError(
                "Refusing to run the tests: " + "; ".join(problems)
                + ". Set TEST_PG_PORT and TEST_REDIS_URL to your throwaway containers, for example "
                "TEST_PG_PORT=55432 and TEST_REDIS_URL=redis://localhost:56379/5, with a local host and a "
                'database name that contains "test". Only CI (the CI variable is set) may use 5432/6379.'
            )
    return expected


def check_settings_match(expected: Mapping[str, str], settings) -> None:
    """Fail if the loaded app settings point anywhere other than the TEST_* databases."""
    resolved = {
        "POSTGRES_SERVER": settings.postgres_server,
        "POSTGRES_PORT": settings.postgres_port,
        "POSTGRES_DB": settings.postgres_db,
        "REDIS_URL": settings.redis_url,
    }
    wrong = [name for name, value in resolved.items() if str(value) != expected[name]]
    if wrong:
        raise RuntimeError(
            "The app settings do not match the TEST_* values for: " + ", ".join(wrong)
            + ". The app would not use the test Postgres and Redis."
        )


# Dummy provider and Google settings. They are assigned, not defaulted, so a value from .env or the shell
# never reaches a test. A real key could not be sent anywhere anyway: see block_real_provider_calls below.
DUMMY_PROVIDER_ENV = {
    "RAZORPAY_KEY_ID": "rzp_test_dummy",
    "RAZORPAY_KEY_SECRET": "dummy-razorpay-secret",
    "RAZORPAY_WEBHOOK_SECRET": "dummy-razorpay-webhook-secret",
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_PUBLISHABLE_KEY": "pk_test_dummy",
    "STRIPE_WEBHOOK_SECRET": "whsec_dummy",
    "GOOGLE_CLIENT_ID": "dummy-google-client-id",
    "GOOGLE_CLIENT_SECRET": "dummy-google-client-secret",
    "PAYMENT_PROVIDER": "razorpay",
    "PAYMENT_CURRENCY": "INR",
}

# This must run before `app` is imported, because the app reads its settings at import time.
_expected_env = resolve_test_env(os.environ)
os.environ.update(_expected_env)
os.environ.update(DUMMY_PROVIDER_ENV)

import pytest  # noqa: E402
import razorpay  # noqa: E402
import stripe  # noqa: E402

from app.core.cache import redis_cache  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.messaging import rabbitmq_client  # noqa: E402
from app.main import app  # noqa: E402

check_settings_match(_expected_env, settings)


def _refuse_real_provider_call(*args, **kwargs):
    raise AssertionError(
        "A real provider call was attempted in a test. Patch the app's own helper "
        "(for example payments._create_provider_order) instead."
    )


@pytest.fixture(autouse=True)
def block_real_provider_calls(monkeypatch):
    monkeypatch.setattr(razorpay, "Client", _refuse_real_provider_call)
    monkeypatch.setattr(stripe.PaymentIntent, "create", _refuse_real_provider_call)


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
