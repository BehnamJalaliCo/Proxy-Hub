"""reality: VLESS + REALITY keys per slot/node, short-lived configs, usage and node status (D18)

* ``reality_keys``: one key pair per scope (a slot, or a node without a slot); the private key is
  in the vault only.
* ``reality_configs``: the configs posts publish (24 h / 50 GB by default), with a fixed link host.
* ``reality_usage``: per config and node, the agent's cumulative bytes (aggregate, no person).
* ``reality_node_status``: each node's Xray state as its agent reports it.

Revision ID: 0015_reality
Revises: 0010_domains
Create Date: 2026-11-04 21:30:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015_reality"
down_revision = "0010_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reality_keys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("public_key", sa.String(length=43), nullable=False),
        sa.Column("short_id", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=260), nullable=False),
        sa.Column("server_name", sa.String(length=253), nullable=False),
        sa.Column("secret_name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(slot IS NULL) <> (node_id IS NULL)", name=op.f("ck_reality_keys_one_scope")),
        sa.CheckConstraint("slot IS NULL OR slot >= 1", name=op.f("ck_reality_keys_slot_positive")),
        sa.CheckConstraint("public_key ~ '^[A-Za-z0-9_-]{43}$'", name=op.f("ck_reality_keys_public_key")),
        sa.CheckConstraint("short_id ~ '^([0-9a-f]{2}){1,8}$'", name=op.f("ck_reality_keys_short_id")),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_reality_keys_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reality_keys")),
        sa.UniqueConstraint("node_id", name=op.f("uq_reality_keys_node_id")),
        sa.UniqueConstraint("secret_name", name=op.f("uq_reality_keys_secret_name")),
        sa.UniqueConstraint("slot", name=op.f("uq_reality_keys_slot")),
    )
    op.create_table(
        "reality_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.BigInteger(), nullable=False),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cap_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'full', 'burned')", name=op.f("ck_reality_configs_status")
        ),
        sa.CheckConstraint("(status = 'active') = (ended_at IS NULL)", name=op.f("ck_reality_configs_ended_pair")),
        sa.CheckConstraint("data_cap_bytes > 0 AND used_bytes >= 0", name=op.f("ck_reality_configs_bytes")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_reality_configs_window")),
        sa.CheckConstraint("slot IS NULL OR slot >= 1", name=op.f("ck_reality_configs_slot_positive")),
        sa.ForeignKeyConstraint(
            ["key_id"],
            ["reality_keys.id"],
            name=op.f("fk_reality_configs_key_id_reality_keys"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_reality_configs_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reality_configs")),
        sa.UniqueConstraint("uuid", name=op.f("uq_reality_configs_uuid")),
    )
    op.create_index("ix_reality_configs_key_status", "reality_configs", ["key_id", "status"], unique=False)
    op.create_index(op.f("ix_reality_configs_node_id"), "reality_configs", ["node_id"], unique=False)
    op.create_index("ix_reality_configs_status_ended", "reality_configs", ["status", "ended_at"], unique=False)
    op.create_table(
        "reality_usage",
        sa.Column("config_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_reported", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("total_bytes >= 0 AND last_reported >= 0", name=op.f("ck_reality_usage_bytes")),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["reality_configs.id"],
            name=op.f("fk_reality_usage_config_id_reality_configs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_reality_usage_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("config_id", "node_id", name=op.f("pk_reality_usage")),
    )
    op.create_table(
        "reality_node_status",
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("up", sa.Boolean(), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=True),
        sa.Column("uptime_s", sa.Float(), nullable=True),
        sa.Column("target_ok", sa.Boolean(), nullable=True),
        sa.Column("apply_error", sa.String(length=60), nullable=True),
        sa.Column("users_reported", sa.Integer(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("users_reported >= 0", name=op.f("ck_reality_node_status_users")),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_reality_node_status_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_reality_node_status")),
    )


def downgrade() -> None:
    op.drop_table("reality_node_status")
    op.drop_table("reality_usage")
    op.drop_index("ix_reality_configs_status_ended", table_name="reality_configs")
    op.drop_index(op.f("ix_reality_configs_node_id"), table_name="reality_configs")
    op.drop_index("ix_reality_configs_key_status", table_name="reality_configs")
    op.drop_table("reality_configs")
    op.drop_table("reality_keys")
