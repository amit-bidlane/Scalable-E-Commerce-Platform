"""products.inventory_count can never be negative

Revision ID: 20261123_0004
Revises: 20261116_0003
Create Date: 2026-11-23 00:00:00.000000
"""

from alembic import op


revision = "20261123_0004"
down_revision = "20261116_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD CONSTRAINT checks every existing row and aborts the migration if one is negative.
    # It is deliberately not NOT VALID, and this migration never changes data: fix such rows by hand,
    # then run the migration again.
    op.create_check_constraint(
        "ck_products_inventory_non_negative",
        "products",
        "inventory_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_products_inventory_non_negative", "products", type_="check")
