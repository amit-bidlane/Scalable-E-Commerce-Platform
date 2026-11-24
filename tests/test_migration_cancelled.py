"""Migration 20261116_0003: the CANCELLED order state and the payment-fields CHECK.

Runs alembic as a subprocess against a scratch database on the throwaway Postgres from the TEST_*
settings (see test_regressions.py). The scratch database is dropped and recreated for every test, and
its name contains "test". Nothing here touches any other database.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime

import psycopg
import pytest

from tests.test_regressions import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, REPO_ROOT

PREVIOUS = "20260423_0002"
REVISION = "20261116_0003"
CONSTRAINT = "ck_orders_payment_fields_together"
SCRATCH_DB = "ecommerce_test_migration_cancelled"


# -------------------------------------------------------------------------- helpers


def connect(dbname: str) -> psycopg.Connection:
    return psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=dbname, autocommit=True
    )


def alembic(*args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "POSTGRES_SERVER": PG_HOST,
        "POSTGRES_PORT": PG_PORT,
        "POSTGRES_USER": PG_USER,
        "POSTGRES_PASSWORD": PG_PASSWORD,
        "POSTGRES_DB": SCRATCH_DB,
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )


def seed_user(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO users (email, hashed_password, role) VALUES ('m@example.com', 'x', 'customer') RETURNING id"
    ).fetchone()
    return row[0]


def insert_order(conn, user_id: int, *, status: str, provider_order_id: str, payment_id=None, paid_at=None) -> None:
    conn.execute(
        "INSERT INTO orders (user_id, status, total_amount, shipping_address, payment_provider,"
        " provider_order_id, provider_payment_id, paid_at)"
        " VALUES (%s, %s::order_state, 10.00, '12 Test Street', 'razorpay', %s, %s, %s)",
        (user_id, status, provider_order_id, payment_id, paid_at),
    )


def enum_labels(conn: psycopg.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT unnest(enum_range(NULL::order_state))::text")]


def version(conn: psycopg.Connection) -> str:
    return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def constraint_exists(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (CONSTRAINT,)).fetchone() is not None


@pytest.fixture
def scratch_db():
    def recreate(create: bool) -> None:
        with connect("postgres") as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
            if create:
                admin.execute(f'CREATE DATABASE "{SCRATCH_DB}"')

    recreate(create=True)
    yield SCRATCH_DB
    recreate(create=False)


# --------------------------------------------------------------------------- tests


def test_upgrade_adds_cancelled_and_keeps_existing_rows(scratch_db) -> None:
    assert alembic("upgrade", PREVIOUS).returncode == 0
    with connect(scratch_db) as conn:
        user_id = seed_user(conn)
        insert_order(conn, user_id, status="PENDING", provider_order_id="order_a")
        insert_order(
            conn, user_id, status="PAID", provider_order_id="order_b", payment_id="pay_b", paid_at=datetime.now(UTC)
        )

    result = alembic("upgrade", REVISION)

    assert result.returncode == 0, result.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == REVISION
        assert "CANCELLED" in enum_labels(conn)
        assert [r[0] for r in conn.execute("SELECT status::text FROM orders ORDER BY id")] == ["PENDING", "PAID"]
        insert_order(
            conn,
            user_id,
            status="CANCELLED",
            provider_order_id="order_c",
            payment_id="pay_c",
            paid_at=datetime.now(UTC),
        )
        refunds = conn.execute(
            "SELECT provider_order_id FROM orders WHERE status = 'CANCELLED' AND paid_at IS NOT NULL"
        ).fetchall()
        assert refunds == [("order_c",)]


def test_downgrade_keeps_the_enum_value_and_upgrade_works_again(scratch_db) -> None:
    """Postgres cannot remove an enum value, so a downgrade leaves CANCELLED in place. It drops the CHECK."""
    assert alembic("upgrade", REVISION).returncode == 0

    result = alembic("downgrade", PREVIOUS)

    assert result.returncode == 0, result.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == PREVIOUS
        assert "CANCELLED" in enum_labels(conn)
        assert not constraint_exists(conn)
    again = alembic("upgrade", REVISION)
    assert again.returncode == 0, again.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert constraint_exists(conn)


@pytest.mark.parametrize(
    ("payment_id", "paid_at"),
    [("pay_half", None), (None, datetime(2026, 1, 1, tzinfo=UTC))],
)
def test_check_rejects_a_payment_id_without_paid_at_and_the_reverse(scratch_db, payment_id, paid_at) -> None:
    assert alembic("upgrade", REVISION).returncode == 0
    with connect(scratch_db) as conn:
        user_id = seed_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation, match=CONSTRAINT):
            insert_order(
                conn, user_id, status="PENDING", provider_order_id="order_half", payment_id=payment_id, paid_at=paid_at
            )


def test_upgrade_fails_loudly_on_rows_that_break_the_check_and_does_not_fix_them(scratch_db) -> None:
    assert alembic("upgrade", PREVIOUS).returncode == 0
    with connect(scratch_db) as conn:
        user_id = seed_user(conn)
        insert_order(conn, user_id, status="PAID", provider_order_id="order_bad", payment_id="pay_bad", paid_at=None)

    result = alembic("upgrade", REVISION)

    assert result.returncode != 0
    assert CONSTRAINT in result.stderr
    with connect(scratch_db) as conn:
        assert version(conn) == PREVIOUS  # the migration is not recorded as done
        assert not constraint_exists(conn)
        row = conn.execute("SELECT provider_payment_id, paid_at FROM orders").fetchone()
        assert row == ("pay_bad", None)  # the migration did not touch the data
        conn.execute("UPDATE orders SET paid_at = now()")  # the operator fixes it by hand

    retry = alembic("upgrade", REVISION)

    assert retry.returncode == 0, retry.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == REVISION
        assert constraint_exists(conn)
