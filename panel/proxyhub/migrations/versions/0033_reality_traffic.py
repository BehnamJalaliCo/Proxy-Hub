"""per node and hour, the Reality bytes up and down (decision D61, the download check)

* ``reality_node_status.traffic_up`` / ``traffic_down``: the node's last cumulative totals of each
  direction over its Reality users (agent 0.5.0 on); the deltas go to
* ``reality_traffic_hours`` (node, hour): bytes the node's users sent (up) and got (down) in that
  hour. Two numbers per node and hour, never per person (rule 7); kept 8 days.

Revision ID: 0033_reality_traffic
Revises: 0032_limit_learned_at
Create Date: 2026-11-08 02:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_reality_traffic"
down_revision = "0032_limit_learned_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reality_node_status", sa.Column("traffic_up", sa.BigInteger(), nullable=True))
    op.add_column("reality_node_status", sa.Column("traffic_down", sa.BigInteger(), nullable=True))
    op.create_table(
        "reality_traffic_hours",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("up", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("down", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("up >= 0 AND down >= 0", name=op.f("ck_reality_traffic_hours_bytes")),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_reality_traffic_hours_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id", "hour", name=op.f("pk_reality_traffic_hours")),
    )


def downgrade() -> None:
    op.drop_table("reality_traffic_hours")
    op.drop_column("reality_node_status", "traffic_down")
    op.drop_column("reality_node_status", "traffic_up")
