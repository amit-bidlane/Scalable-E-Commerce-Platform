import pytest

from app.core.config import Settings

SECRET_FIELDS = ("jwt_secret_key", "jwt_refresh_secret_key", "postgres_password", "rabbitmq_url", "redis_url")

# The real defaults from Settings, so these tests break if a default changes.
DEFAULTS = {name: Settings.model_fields[name].default for name in SECRET_FIELDS}

GOOD = {
    "jwt_secret_key": "access-secret-4f9a1c",
    "jwt_refresh_secret_key": "refresh-secret-7b2d8e",
    "postgres_password": "pg-secret-3e6f0a",
    "rabbitmq_url": "amqp://shop:rmq-secret-5c1d@rabbitmq:5672/",
    "redis_url": "redis://:redis-secret-9a4b@redis:6379/0",
}


def make(app_env: str, **values: str) -> Settings:
    # _env_file=None and explicit values: the result must not depend on a local .env or CI variables.
    return Settings(_env_file=None, app_env=app_env, **values)


def test_production_with_all_defaults_is_refused() -> None:
    with pytest.raises(RuntimeError) as exc:
        make("production", **DEFAULTS)

    message = str(exc.value)
    for name in SECRET_FIELDS:
        assert name in message


def test_production_with_good_values_starts() -> None:
    settings = make("production", **GOOD)

    assert settings.app_env == "production"


@pytest.mark.parametrize("env", ["development", "test"])
def test_other_environments_still_accept_the_defaults(env: str) -> None:
    settings = make(env, **DEFAULTS)

    assert settings.app_env == env


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_production_names_the_one_field_that_is_still_a_default(field: str) -> None:
    values = {**GOOD, field: DEFAULTS[field]}

    with pytest.raises(RuntimeError) as exc:
        make("production", **values)

    message = str(exc.value)
    assert field in message
    assert all(other not in message for other in SECRET_FIELDS if other != field)


@pytest.mark.parametrize(
    ("field", "empty_value"),
    [
        ("jwt_secret_key", ""),
        ("jwt_refresh_secret_key", "   "),
        ("postgres_password", ""),
        ("rabbitmq_url", "amqp://shop:@rabbitmq:5672/"),
        ("redis_url", "redis://redis:6379/0"),
    ],
)
def test_production_refuses_empty_secrets(field: str, empty_value: str) -> None:
    with pytest.raises(RuntimeError) as exc:
        make("production", **{**GOOD, field: empty_value})

    assert field in str(exc.value)


@pytest.mark.parametrize(
    ("field", "placeholder"),
    [
        ("jwt_secret_key", "change-me-abc123"),
        ("jwt_refresh_secret_key", "ChangeMe"),
        ("postgres_password", "postgres"),
        ("rabbitmq_url", "amqp://shop:change-me@rabbitmq:5672/"),
        ("redis_url", "redis://:changeme@redis:6379/0"),
    ],
)
def test_production_refuses_placeholders(field: str, placeholder: str) -> None:
    with pytest.raises(RuntimeError) as exc:
        make("production", **{**GOOD, field: placeholder})

    assert field in str(exc.value)


def test_production_refuses_the_guest_rabbitmq_user_even_with_a_strong_password() -> None:
    with pytest.raises(RuntimeError) as exc:
        make("production", **{**GOOD, "rabbitmq_url": "amqp://guest:strong-LEAK-pass@rabbitmq:5672/"})

    message = str(exc.value)
    assert "rabbitmq_url" in message
    assert "LEAK" not in message


def test_the_error_message_never_contains_a_secret_value() -> None:
    bad = {
        "jwt_secret_key": "change-me-LEAK1",
        "jwt_refresh_secret_key": "changeme-LEAK2",
        "postgres_password": "change-me-LEAK6",
        "rabbitmq_url": "amqp://guest:LEAK3pass@rabbitmq:5672/",
        "redis_url": "redis://:change-me-LEAK4@redis:6379/0",
    }

    with pytest.raises(RuntimeError) as exc:
        make("production", **bad)

    text = f"{exc.value} {exc.value!r} {exc.value.args} {exc.value.__cause__!r} {exc.value.__context__!r}"
    for leaked in ("LEAK1", "LEAK2", "LEAK3", "LEAK4", "LEAK6", "change-me", "changeme", "guest"):
        assert leaked not in text


@pytest.mark.parametrize("env", ["prod", "staging", "", "prod-LEAK5"])
def test_an_unknown_app_env_is_refused_and_not_printed(env: str) -> None:
    with pytest.raises(RuntimeError) as exc:
        make(env, **GOOD)

    message = str(exc.value)
    assert "app_env" in message
    assert "LEAK5" not in message


@pytest.mark.parametrize("env", ["Production", " PRODUCTION "])
def test_app_env_is_cleaned_up_and_treated_as_production(env: str) -> None:
    with pytest.raises(RuntimeError):
        make(env, **DEFAULTS)

    assert make(env, **GOOD).app_env == "production"
