"""The test run must not read payment-provider or Google credentials from .env or the shell, and must not
call a real provider.

conftest.py sets dummy values before the app loads, and an autouse fixture makes razorpay.Client and
stripe.PaymentIntent.create raise. Tests that need a provider answer patch the app's own helper instead.
"""

import pytest
import razorpay
import stripe

from app.core.config import settings

# Written out here on purpose, not imported from conftest: this test checks what conftest produced.
DUMMY = {
    "razorpay_key_id": "rzp_test_dummy",
    "razorpay_key_secret": "dummy-razorpay-secret",
    "razorpay_webhook_secret": "dummy-razorpay-webhook-secret",
    "stripe_secret_key": "sk_test_dummy",
    "stripe_publishable_key": "pk_test_dummy",
    "stripe_webhook_secret": "whsec_dummy",
    "google_client_id": "dummy-google-client-id",
    "google_client_secret": "dummy-google-client-secret",
    "payment_provider": "razorpay",
    "payment_currency": "INR",
}


def test_the_loaded_settings_hold_the_dummy_provider_values() -> None:
    loaded = {name: getattr(settings, name) for name in DUMMY}

    assert loaded == DUMMY


def test_a_real_razorpay_call_is_blocked() -> None:
    with pytest.raises(AssertionError, match="real provider call"):
        razorpay.Client(auth=("id", "secret"))


def test_a_real_stripe_call_is_blocked() -> None:
    with pytest.raises(AssertionError, match="real provider call"):
        stripe.PaymentIntent.create(amount=100, currency="inr")
