"""Unit tests for the test-environment guard in conftest. Pure functions: no database, no Redis.

Each test passes its own env mapping, so nothing here depends on the real environment.
"""

from types import SimpleNamespace

import pytest

from tests.conftest import check_settings_match, resolve_test_env

SAFE = {"TEST_PG_PORT": "55432", "TEST_REDIS_URL": "redis://localhost:56379/5"}


def refusal(env: dict[str, str]) -> str:
    with pytest.raises(RuntimeError) as excinfo:
        resolve_test_env(env)
    return str(excinfo.value)


def test_default_postgres_port_is_refused_without_ci() -> None:
    message = refusal({**SAFE, "TEST_PG_PORT": "5432"})

    assert "default Postgres port" in message
    assert "default Redis port" not in message


def test_unset_environment_is_refused_with_a_hint() -> None:
    message = refusal({})

    assert "default Postgres port" in message
    assert "default Redis port" in message
    assert "Set TEST_PG_PORT and TEST_REDIS_URL" in message


def test_default_redis_port_is_refused_without_ci() -> None:
    message = refusal({**SAFE, "TEST_REDIS_URL": "redis://localhost:6379/5"})

    assert "default Redis port" in message
    assert "default Postgres port" not in message


def test_redis_url_without_a_port_counts_as_6379() -> None:
    assert "default Redis port" in refusal({**SAFE, "TEST_REDIS_URL": "redis://localhost/5"})


def test_non_local_postgres_host_is_refused_without_ci() -> None:
    message = refusal({**SAFE, "TEST_PG_HOST": "db"})

    assert "TEST_PG_HOST is not a local host" in message
    assert "TEST_REDIS_URL host is not a local host" not in message


def test_non_local_redis_host_is_refused_without_ci() -> None:
    message = refusal({**SAFE, "TEST_REDIS_URL": "redis://redis:56379/5"})

    assert "TEST_REDIS_URL host is not a local host" in message
    assert "TEST_PG_HOST is not a local host" not in message


def test_database_name_without_test_is_refused_without_ci() -> None:
    message = refusal({**SAFE, "TEST_PG_DB": "ecommerce"})

    assert 'does not contain "test"' in message
    assert "default" not in message


def test_database_name_check_ignores_case() -> None:
    expected = resolve_test_env({**SAFE, "TEST_PG_DB": "ECOMMERCE_TEST"})

    assert expected["POSTGRES_DB"] == "ECOMMERCE_TEST"


def test_two_problems_are_both_listed() -> None:
    message = refusal({**SAFE, "TEST_PG_PORT": "5432", "TEST_PG_DB": "ecommerce"})

    assert "default Postgres port" in message
    assert 'does not contain "test"' in message


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_local_postgres_hosts_pass(host: str) -> None:
    assert resolve_test_env({**SAFE, "TEST_PG_HOST": host})["POSTGRES_SERVER"] == host


@pytest.mark.parametrize("url", ["redis://localhost:56379/5", "redis://127.0.0.1:56379/5", "redis://[::1]:56379/5"])
def test_local_redis_hosts_pass(url: str) -> None:
    assert resolve_test_env({**SAFE, "TEST_REDIS_URL": url})["REDIS_URL"] == url


def test_default_ports_and_other_hosts_are_allowed_in_ci() -> None:
    env = {
        "CI": "true",
        "TEST_PG_HOST": "db",
        "TEST_PG_PORT": "5432",
        "TEST_REDIS_URL": "redis://redis:6379/5",
    }

    expected = resolve_test_env(env)

    assert expected["POSTGRES_SERVER"] == "db"
    assert expected["POSTGRES_PORT"] == "5432"
    assert expected["REDIS_URL"] == "redis://redis:6379/5"


def test_throwaway_values_pass_and_are_copied_to_the_app_variables() -> None:
    expected = resolve_test_env(
        {
            **SAFE,
            "TEST_PG_HOST": "127.0.0.1",
            "TEST_PG_USER": "tester",
            "TEST_PG_PASSWORD": "secret",
            "TEST_PG_DB": "shop_test",
        }
    )

    assert expected == {
        "POSTGRES_SERVER": "127.0.0.1",
        "POSTGRES_PORT": "55432",
        "POSTGRES_USER": "tester",
        "POSTGRES_PASSWORD": "secret",
        "POSTGRES_DB": "shop_test",
        "REDIS_URL": "redis://localhost:56379/5",
    }


def make_settings(**overrides) -> SimpleNamespace:
    values = {
        "postgres_server": "localhost",
        "postgres_port": 55432,
        "postgres_db": "ecommerce_test",
        "redis_url": "redis://localhost:56379/5",
    }
    return SimpleNamespace(**{**values, **overrides})


def test_matching_settings_pass() -> None:
    check_settings_match(resolve_test_env(SAFE), make_settings())


@pytest.mark.parametrize(
    ("field", "value", "name"),
    [
        ("postgres_server", "db", "POSTGRES_SERVER"),
        ("postgres_port", 5432, "POSTGRES_PORT"),
        ("postgres_db", "ecommerce", "POSTGRES_DB"),
        ("redis_url", "redis://redis:6379/0", "REDIS_URL"),
    ],
)
def test_settings_that_differ_from_the_test_values_fail(field: str, value, name: str) -> None:
    with pytest.raises(RuntimeError, match=rf"for: {name}\."):
        check_settings_match(resolve_test_env(SAFE), make_settings(**{field: value}))
