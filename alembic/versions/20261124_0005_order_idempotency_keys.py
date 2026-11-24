"""the idempotency_keys table for checkout requests

Revision ID: 20261124_0005
Revises: 20261123_0004
Create Date: 2026-11-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20261124_0005"
down_revision = "20261123_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # order_id is nullable because the key row is inserted before the order exists, in the same
    # transaction. This migration only creates a table and never touches existing rows.
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
