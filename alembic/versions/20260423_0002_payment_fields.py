"""add payment fields to orders

Revision ID: 20260423_0002
Revises: 20260423_0001
Create Date: 2026-04-23 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260423_0002"
down_revision = "20260423_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("payment_provider", sa.String(length=32), nullable=False, server_default="razorpay"),
    )
    op.add_column("orders", sa.Column("provider_order_id", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("provider_payment_id", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_orders_provider_order_id"), "orders", ["provider_order_id"], unique=True)
    op.create_index(op.f("ix_orders_provider_payment_id"), "orders", ["provider_payment_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_orders_provider_payment_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_provider_order_id"), table_name="orders")
    op.drop_column("orders", "paid_at")
    op.drop_column("orders", "provider_payment_id")
    op.drop_column("orders", "provider_order_id")
    op.drop_column("orders", "payment_provider")
