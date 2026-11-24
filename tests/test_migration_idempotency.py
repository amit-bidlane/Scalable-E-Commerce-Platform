"""Migration 20261124_0005 and the IdempotencyKey model: the idempotency_keys table.

The migration tests run alembic as a subprocess against a scratch database on the throwaway Postgres
from the TEST_* settings (see test_regressions.py). The scratch database is dropped and recreated for
every test, and its name contains "test". Nothing here touches any other database.

The last two tests use the schema that the model creates (create_all). One compares it with the schema
the migration creates (columns, constraints, indexes), the other checks that it rejects a duplicate with
the same constraint name, so the model and the migration cannot drift apart without a test failing.

Nothing in this upgrade can fail on existing data (it only creates a table), so there is no "fails
loudly on bad rows" test, unlike test_migration_inventory.py.
"""

import os
import subprocess
import sys

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_regressions import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, REPO_ROOT
from tests.test_regressions import engine, maker  # noqa: F401  (fixtures: the create_all schema)

PREVIOUS = "20261123_0004"
REVISION = "20261124_0005"
TABLE = "idempotency_keys"
UNIQUE = "uq_idempotency_keys_user_key"
SCRATCH_DB = "ecommerce_test_migration_idempotency"
HASH = "a" * 64
CONSTRAINTS = """
    SELECT conname, contype::text, pg_get_constraintdef(oid)
    FROM pg_constraint WHERE conrelid = 'idempotency_keys'::regclass ORDER BY conname
"""
INDEXES = "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'idempotency_keys' ORDER BY indexname"
COLUMNS = """
    SELECT column_name, data_type, character_maximum_length, is_nullable, column_default
    FROM information_schema.columns WHERE table_name = 'idempotency_keys' ORDER BY column_name
"""


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


def insert_user(conn: psycopg.Connection, n: int) -> int:
    return conn.execute(
        "INSERT INTO users (email, hashed_password, role) VALUES (%s, 'x', 'customer') RETURNING id",
        (f"idem-{n}@example.com",),
    ).fetchone()[0]


def insert_order(conn: psycopg.Connection, user_id: int, n: int) -> int:
    return conn.execute(
        "INSERT INTO orders (user_id, status, total_amount, shipping_address, payment_provider,"
        " provider_order_id) VALUES (%s, 'PENDING', 10.00, '12 Test Street', 'razorpay', %s) RETURNING id",
        (user_id, f"order_{n}"),
    ).fetchone()[0]


def insert_key(conn: psycopg.Connection, user_id: int, key: str, order_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys (user_id, key, request_hash, order_id) VALUES (%s, %s, %s, %s)",
        (user_id, key, HASH, order_id),
    )


def keys(conn: psycopg.Connection) -> list[tuple[int, str, int | None]]:
    return conn.execute("SELECT user_id, key, order_id FROM idempotency_keys ORDER BY id").fetchall()


def version(conn: psycopg.Connection) -> str:
    return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def table_exists(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT to_regclass('public.idempotency_keys')").fetchone()[0] is not None


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


def test_upgrade_creates_the_table_and_keeps_existing_orders(scratch_db) -> None:
    assert alembic("upgrade", PREVIOUS).returncode == 0
    with connect(scratch_db) as conn:
        user_id = insert_user(conn, 1)
        order_id = insert_order(conn, user_id, 1)
        assert not table_exists(conn)

    result = alembic("upgrade", REVISION)

    assert result.returncode == 0, result.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == REVISION
        assert table_exists(conn)
        assert conn.execute("SELECT id, user_id FROM orders").fetchall() == [(order_id, user_id)]
        assert keys(conn) == []
        columns = {row[0]: row[1:] for row in conn.execute(COLUMNS)}
        assert set(columns) == {"id", "user_id", "key", "request_hash", "order_id", "created_at"}
        assert columns["key"][1] == 64 and columns["request_hash"][1] == 64  # character_maximum_length
        nullable = {name: column[2] for name, column in columns.items()}  # is_nullable
        # order_id is nullable because the key row is inserted before the order exists.
        assert nullable == {
            "id": "NO",
            "user_id": "NO",
            "key": "NO",
            "request_hash": "NO",
            "order_id": "YES",
            "created_at": "NO",
        }


def test_the_unique_constraint_is_per_user_and_a_key_row_may_have_no_order_yet(scratch_db) -> None:
    assert alembic("upgrade", REVISION).returncode == 0
    with connect(scratch_db) as conn:
        first = insert_user(conn, 1)
        second = insert_user(conn, 2)
        insert_key(conn, first, "key-0000001")  # order_id NULL is accepted

        with pytest.raises(psycopg.errors.UniqueViolation, match=UNIQUE):
            insert_key(conn, first, "key-0000001")
        insert_key(conn, second, "key-0000001")  # the same key for another user is independent
        insert_key(conn, first, "key-0000002")  # another key for the same user is independent

        assert keys(conn) == [
            (first, "key-0000001", None),
            (second, "key-0000001", None),
            (first, "key-0000002", None),
        ]
        with pytest.raises(psycopg.errors.StringDataRightTruncation):
            insert_key(conn, first, "k" * 65)


def test_foreign_keys_reject_unknown_rows_and_cascade_on_delete(scratch_db) -> None:
    assert alembic("upgrade", REVISION).returncode == 0
    with connect(scratch_db) as conn:
        user_id = insert_user(conn, 1)
        order_id = insert_order(conn, user_id, 1)
        other_user = insert_user(conn, 2)

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            insert_key(conn, user_id + 1000, "key-0000001")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            insert_key(conn, user_id, "key-0000001", order_id=order_id + 1000)

        insert_key(conn, user_id, "key-0000001", order_id=order_id)
        conn.execute("DELETE FROM orders WHERE id = %s", (order_id,))
        assert keys(conn) == []  # deleting the order deletes its key row

        insert_key(conn, other_user, "key-0000002")
        conn.execute("DELETE FROM users WHERE id = %s", (other_user,))
        assert keys(conn) == []  # deleting the user deletes their key rows


def test_downgrade_drops_the_table_and_keeps_orders_and_upgrade_works_again(scratch_db) -> None:
    assert alembic("upgrade", REVISION).returncode == 0
    with connect(scratch_db) as conn:
        user_id = insert_user(conn, 1)
        order_id = insert_order(conn, user_id, 1)
        insert_key(conn, user_id, "key-0000001", order_id=order_id)

    result = alembic("downgrade", PREVIOUS)

    assert result.returncode == 0, result.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == PREVIOUS
        assert not table_exists(conn)
        assert conn.execute("SELECT id FROM orders").fetchall() == [(order_id,)]

    again = alembic("upgrade", REVISION)

    assert again.returncode == 0, again.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == REVISION
        assert table_exists(conn)
        assert keys(conn) == []


async def test_the_model_schema_equals_the_migration_schema(scratch_db, maker) -> None:
    """Columns, constraints (names and definitions, including the ON DELETE rules) and indexes. The
    migration side comes from alembic, the model side from Base.metadata.create_all."""
    assert alembic("upgrade", REVISION).returncode == 0
    with connect(scratch_db) as conn:
        migration = {
            "columns": conn.execute(COLUMNS).fetchall(),
            "constraints": conn.execute(CONSTRAINTS).fetchall(),
            "indexes": conn.execute(INDEXES).fetchall(),
        }
    assert UNIQUE in [row[0] for row in migration["constraints"]]

    async with maker() as session:
        model = {
            "columns": [tuple(row) for row in (await session.execute(text(COLUMNS))).all()],
            "constraints": [tuple(row) for row in (await session.execute(text(CONSTRAINTS))).all()],
            "indexes": [tuple(row) for row in (await session.execute(text(INDEXES))).all()],
        }

    assert model == migration


async def test_the_model_schema_rejects_a_duplicate_key_with_the_same_constraint(maker) -> None:
    """The schema here comes from Base.metadata.create_all, not from alembic."""
    from app.models.order import IdempotencyKey
    from app.models.user import User

    async with maker() as session:
        user = User(email="idem-model@example.com", hashed_password="x")
        session.add(user)
        await session.flush()
        session.add(IdempotencyKey(user_id=user.id, key="key-0000001", request_hash=HASH))
        await session.commit()
        user_id = user.id

    async with maker() as session:
        session.add(IdempotencyKey(user_id=user_id, key="key-0000001", request_hash=HASH))
        with pytest.raises(IntegrityError, match=UNIQUE):
            await session.commit()
