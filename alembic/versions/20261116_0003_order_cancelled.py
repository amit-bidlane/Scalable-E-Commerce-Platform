"""add the CANCELLED order state and the payment fields check

Revision ID: 20261116_0003
Revises: 20260423_0002
Create Date: 2026-11-16 00:00:00.000000
"""

from alembic import op


revision = "20261116_0003"
down_revision = "20260423_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Run outside the migration transaction: Postgres does not let the transaction that adds an enum
    # value use it. IF NOT EXISTS makes a re-run safe, which matters because the value stays added when
    # the CHECK below fails and the migration has to be run again.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE order_state ADD VALUE IF NOT EXISTS 'CANCELLED'")

    # A plain ADD CONSTRAINT checks every existing row and aborts the migration if one breaks the rule.
    # It is deliberately not NOT VALID, and this migration never changes data: fix such rows by hand,
    # then run the migration again.
    op.create_check_constraint(
        "ck_orders_payment_fields_together",
        "orders",
        "(provider_payment_id IS NULL) = (paid_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_orders_payment_fields_together", "orders", type_="check")
    # Postgres cannot remove a value from an enum type, so 'CANCELLED' stays in order_state after a
    # downgrade. Orders that already have that status keep it.
