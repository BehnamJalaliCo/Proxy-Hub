"""fleet: secret vault, account observations, live offers, dry-run plans

Revision ID: 0003_fleet
Revises: 0001
Create Date: 2026-11-03 13:25:23.849482
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_fleet"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("key_version >= 1", name=op.f("ck_secrets_key_version")),
        sa.CheckConstraint("octet_length(ciphertext) >= 16", name=op.f("ck_secrets_ciphertext_len")),
        sa.CheckConstraint("octet_length(nonce) = 12", name=op.f("ck_secrets_nonce_len")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secrets")),
        sa.UniqueConstraint("name", name=op.f("uq_secrets_name")),
    )
    op.create_table(
        "hetzner_account_observations",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("guard_ok", sa.Boolean(), nullable=True),
        sa.Column("servers_total", sa.Integer(), nullable=True),
        sa.Column("managed_servers", sa.Integer(), nullable=True),
        sa.Column("foreign_servers", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=60), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["hetzner_accounts.id"],
            name=op.f("fk_hetzner_account_observations_account_id_hetzner_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_hetzner_account_observations")),
    )
    op.create_table(
        "hetzner_account_servers",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("hetzner_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delete_protected", sa.Boolean(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["hetzner_accounts.id"],
            name=op.f("fk_hetzner_account_servers_account_id_hetzner_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "hetzner_id", name=op.f("pk_hetzner_account_servers")),
    )
    op.create_table(
        "hetzner_offers",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("server_type", sa.String(length=40), nullable=False),
        sa.Column("location", sa.String(length=20), nullable=False),
        sa.Column("price_hourly_net", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("price_monthly_net", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("included_traffic_bytes", sa.BigInteger(), nullable=True),
        sa.Column("deprecated", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "price_hourly_net >= 0 AND price_monthly_net >= 0",
            name=op.f("ck_hetzner_offers_price_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["hetzner_accounts.id"],
            name=op.f("fk_hetzner_offers_account_id_hetzner_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "server_type", "location", name=op.f("pk_hetzner_offers")),
    )
    op.create_table(
        "fleet_plans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("logged", sa.Boolean(), nullable=False),
        sa.Column("actions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fleet_plans")),
    )
    op.create_index("ix_fleet_plans_generated_at", "fleet_plans", ["generated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_fleet_plans_generated_at", table_name="fleet_plans")
    op.drop_table("fleet_plans")
    op.drop_table("hetzner_offers")
    op.drop_table("hetzner_account_servers")
    op.drop_table("hetzner_account_observations")
    op.drop_table("secrets")
