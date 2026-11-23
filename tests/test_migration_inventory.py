"""Migration 20261123_0004 and the Product model: products.inventory_count can never be negative.

The migration tests run alembic as a subprocess against a scratch database on the throwaway Postgres
from the TEST_* settings (see test_regressions.py). The scratch database is dropped and recreated for
every test, and its name contains "test". Nothing here touches any other database.

The last test uses the schema that the model creates (create_all), so the model and the migration
cannot drift apart without a test failing.
"""

import os
import subprocess
import sys
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_regressions import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, REPO_ROOT
from tests.test_regressions import engine, maker  # noqa: F401  (fixtures: the create_all schema)

PREVIOUS = "20261116_0003"
REVISION = "20261123_0004"
CONSTRAINT = "ck_products_inventory_non_negative"
SCRATCH_DB = "ecommerce_test_migration_inventory"


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


def insert_product(conn: psycopg.Connection, n: int, stock: int) -> None:
    conn.execute(
        "INSERT INTO products (name, slug, sku, price, inventory_count) VALUES (%s, %s, %s, 10.00, %s)",
        (f"Product {n}", f"product-{n}", f"SKU-{n}", stock),
    )


def stocks(conn: psycopg.Connection) -> list[int]:
    return [row[0] for row in conn.execute("SELECT inventory_count FROM products ORDER BY id")]


def version(conn: psycopg.Connection) -> str:
    return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def constraint_exists(conn: psycopg.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = 'products'::regclass", (CONSTRAINT,)
        ).fetchone()
        is not None
    )


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


def test_upgrade_adds_the_check_and_keeps_existing_rows(scratch_db) -> None:
    assert alembic("upgrade", PREVIOUS).returncode == 0
    with connect(scratch_db) as conn:
        insert_product(conn, 1, 5)
        insert_product(conn, 2, 0)

    result = alembic("upgrade", REVISION)

    assert result.returncode == 0, result.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == REVISION
        assert constraint_exists(conn)
        assert stocks(conn) == [5, 0]


def test_check_rejects_a_negative_inventory_count_on_insert_and_on_update(scratch_db) -> None:
    assert alembic("upgrade", REVISION).returncode == 0
    with connect(scratch_db) as conn:
        insert_product(conn, 1, 0)  # zero is allowed
        with pytest.raises(psycopg.errors.CheckViolation, match=CONSTRAINT):
            insert_product(conn, 2, -1)
        with pytest.raises(psycopg.errors.CheckViolation, match=CONSTRAINT):
            conn.execute("UPDATE products SET inventory_count = -1")
        assert stocks(conn) == [0]


def test_upgrade_fails_loudly_on_negative_rows_and_does_not_fix_them(scratch_db) -> None:
    assert alembic("upgrade", PREVIOUS).returncode == 0
    with connect(scratch_db) as conn:
        insert_product(conn, 1, 4)
        insert_product(conn, 2, -3)

    result = alembic("upgrade", REVISION)

    assert result.returncode != 0
    assert CONSTRAINT in result.stderr
    with connect(scratch_db) as conn:
        assert version(conn) == PREVIOUS  # the migration is not recorded as done
        assert not constraint_exists(conn)
        assert stocks(conn) == [4, -3]  # the migration did not touch the data
        conn.execute("UPDATE products SET inventory_count = 0 WHERE inventory_count < 0")  # fixed by hand

    retry = alembic("upgrade", REVISION)

    assert retry.returncode == 0, retry.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == REVISION
        assert constraint_exists(conn)
        assert stocks(conn) == [4, 0]


def test_downgrade_drops_the_check(scratch_db) -> None:
    assert alembic("upgrade", REVISION).returncode == 0

    result = alembic("downgrade", PREVIOUS)

    assert result.returncode == 0, result.stderr[-2000:]
    with connect(scratch_db) as conn:
        assert version(conn) == PREVIOUS
        assert not constraint_exists(conn)
        insert_product(conn, 1, -1)  # accepted again, which proves the CHECK is gone
        assert stocks(conn) == [-1]


async def test_the_model_schema_rejects_a_negative_inventory_count_with_the_same_constraint(maker) -> None:
    """The schema here comes from Base.metadata.create_all, not from alembic."""
    from app.models.product import Product

    async with maker() as session:
        product = Product(name="P", slug="p", sku="P-1", price=Decimal("10.00"), inventory_count=3)
        session.add(product)
        await session.commit()
        product_id = product.id

    async with maker() as session:
        with pytest.raises(IntegrityError, match=CONSTRAINT):
            await session.execute(
                text("UPDATE products SET inventory_count = -1 WHERE id = :id"), {"id": product_id}
            )
