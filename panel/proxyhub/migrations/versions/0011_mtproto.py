"""mtproto: the long-lived Telemt user (public MTProto proxy) the policy keeps on each node

* ``mtproto_proxies``: one row per node, naming the node's public proxy user in ``telemt_users``
  (the secret stays sealed there), when the agent first ran it (``live_at``) and when the policy
  disabled it because the node left service (``disabled_at``). No end-user data (rule 7).

Revision ID: 0011_mtproto
Revises: 0010_domains
Create Date: 2026-11-04 19:30:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_mtproto"
down_revision = "0010_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mtproto_proxies",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("user_name", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("live_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "live_at IS NULL OR live_at >= created_at", name=op.f("ck_mtproto_proxies_live_after_created")
        ),
        sa.CheckConstraint("user_name ~ '^[a-z][a-z0-9_]{0,31}$'", name=op.f("ck_mtproto_proxies_user_name")),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_mtproto_proxies_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_mtproto_proxies")),
    )


def downgrade() -> None:
    op.drop_table("mtproto_proxies")
