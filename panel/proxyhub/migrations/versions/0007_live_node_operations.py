"""live: node executor bookkeeping (node_operations)

Revision ID: 0007_live
Revises: 0005_merge
Create Date: 2026-11-03 20:25:50.586911
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_live"
down_revision = "0005_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_operations",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("create_attempts", sa.Integer(), nullable=False),
        sa.Column("create_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("server_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retire_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retire_reason", sa.String(length=40), nullable=True),
        sa.Column("delete_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=60), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "retire_reason IS NULL OR retire_reason IN ('owner', 'unhealthy_test_node', 'create_failed')",
            name=op.f("ck_node_operations_retire_reason"),
        ),
        sa.CheckConstraint(
            "(retire_requested_at IS NULL) = (retire_reason IS NULL)",
            name=op.f("ck_node_operations_retire_pair"),
        ),
        sa.CheckConstraint(
            "create_attempts >= 0 AND delete_attempts >= 0", name=op.f("ck_node_operations_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_node_operations_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_node_operations")),
    )


def downgrade() -> None:
    op.drop_table("node_operations")
