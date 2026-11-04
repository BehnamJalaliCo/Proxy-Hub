"""net: Cloudflare DNS and domain pool, reachability probes from Iran

Revision ID: 0008_net
Revises: 0005_merge
Create Date: 2026-11-03 21:16:00.563742
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_net"
down_revision = "0005_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cf_state",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=40), nullable=True),
        sa.Column("missing", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_kind", sa.String(length=10), nullable=True),
        sa.Column("zones_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "token_kind IS NULL OR token_kind IN ('user', 'account')", name=op.f("ck_cf_state_token_kind")
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_cf_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cf_state")),
    )
    op.create_table(
        "cf_zones",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=253), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pool_state", sa.String(length=10), nullable=False),
        sa.Column("pool_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dns_edit", sa.Boolean(), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id ~ '^[0-9a-f]{32}$'", name=op.f("ck_cf_zones_id_hex")),
        sa.CheckConstraint(
            "pool_state IN ('none', 'active', 'standby', 'filtered', 'retired')",
            name=op.f("ck_cf_zones_pool_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cf_zones")),
    )
    op.create_index(
        "uq_cf_zones_one_active",
        "cf_zones",
        ["pool_state"],
        unique=True,
        postgresql_where=sa.text("pool_state = 'active'"),
    )
    op.create_table(
        "probe_cycles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_probe_cycles")),
    )
    op.create_table(
        "probe_sources",
        sa.Column("name", sa.String(length=20), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ratelimit_limit", sa.Integer(), nullable=True),
        sa.Column("ratelimit_remaining", sa.Integer(), nullable=True),
        sa.Column("ratelimit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("name IN ('globalping', 'check-host')", name=op.f("ck_probe_sources_name")),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_probe_sources")),
    )
    op.create_table(
        "cf_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("zone_id", sa.String(length=32), nullable=False),
        sa.Column("cf_record_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=253), nullable=False),
        sa.Column("type", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("ttl", sa.Integer(), nullable=False),
        sa.Column("proxied", sa.Boolean(), nullable=False),
        sa.Column("purpose", sa.String(length=10), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "cf_record_id IS NULL OR cf_record_id ~ '^[0-9a-f]{32}$'", name=op.f("ck_cf_records_cf_id_hex")
        ),
        sa.CheckConstraint("purpose IN ('slot', 'test')", name=op.f("ck_cf_records_purpose")),
        sa.CheckConstraint("type IN ('A', 'AAAA', 'TXT')", name=op.f("ck_cf_records_type")),
        sa.CheckConstraint("ttl = 1 OR ttl BETWEEN 60 AND 86400", name=op.f("ck_cf_records_ttl")),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_cf_records_node_id_nodes"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["zone_id"], ["cf_zones.id"], name=op.f("fk_cf_records_zone_id_cf_zones")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cf_records")),
        sa.UniqueConstraint("cf_record_id", name=op.f("uq_cf_records_cf_record_id")),
        sa.UniqueConstraint("zone_id", "name", "type", name="uq_cf_records_zone_name_type"),
    )
    op.create_table(
        "probe_measurements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("target", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "(role = 'node') = (node_id IS NOT NULL)", name=op.f("ck_probe_measurements_role_node")
        ),
        sa.CheckConstraint("kind IN ('tcp', 'icmp', 'dns')", name=op.f("ck_probe_measurements_kind")),
        sa.CheckConstraint("role IN ('node', 'control')", name=op.f("ck_probe_measurements_role")),
        sa.CheckConstraint(
            "source IN ('globalping', 'check-host')", name=op.f("ck_probe_measurements_source")
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'no_probes', 'rate_limited', 'error', 'timeout', 'skipped')",
            name=op.f("ck_probe_measurements_status"),
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["probe_cycles.id"],
            name=op.f("fk_probe_measurements_cycle_id_probe_cycles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_probe_measurements_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_probe_measurements")),
    )
    op.create_index("ix_probe_measurements_cycle", "probe_measurements", ["cycle_id"], unique=False)
    op.create_index("ix_probe_measurements_node_at", "probe_measurements", ["node_id", "at"], unique=False)
    op.create_table(
        "probe_node_state",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("last_round_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('ok', 'suspect', 'blocked', 'unknown')", name=op.f("ck_probe_node_state_verdict")
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_probe_node_state_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_probe_node_state")),
    )
    op.create_table(
        "probe_rounds",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(length=10), nullable=False),
        sa.Column("target_ip", postgresql.INET(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("observations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("verdict_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("trigger IN ('schedule', 'manual')", name=op.f("ck_probe_rounds_trigger")),
        sa.CheckConstraint(
            "verdict IN ('ok', 'suspect', 'blocked', 'unknown')", name=op.f("ck_probe_rounds_verdict")
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["probe_cycles.id"],
            name=op.f("fk_probe_rounds_cycle_id_probe_cycles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_probe_rounds_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_probe_rounds")),
        sa.UniqueConstraint("cycle_id", "node_id", name="uq_probe_rounds_cycle_node"),
    )
    op.create_index("ix_probe_rounds_node_started", "probe_rounds", ["node_id", "started_at"], unique=False)
    op.create_table(
        "probe_network_results",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("measurement_id", sa.BigInteger(), nullable=False),
        sa.Column("network_key", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=100), nullable=False),
        sa.Column("asn", sa.BigInteger(), nullable=True),
        sa.Column("ok", sa.Integer(), nullable=False),
        sa.Column("fail", sa.Integer(), nullable=False),
        sa.Column("nodata", sa.Integer(), nullable=False),
        sa.Column("rtt_ms", sa.Float(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "ok >= 0 AND fail >= 0 AND nodata >= 0", name=op.f("ck_probe_network_results_counts")
        ),
        sa.ForeignKeyConstraint(
            ["measurement_id"],
            ["probe_measurements.id"],
            name=op.f("fk_probe_network_results_measurement_id_probe_measurements"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_probe_network_results")),
    )
    op.create_index(
        "ix_probe_network_results_measurement", "probe_network_results", ["measurement_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_probe_network_results_measurement", table_name="probe_network_results")
    op.drop_table("probe_network_results")
    op.drop_index("ix_probe_rounds_node_started", table_name="probe_rounds")
    op.drop_table("probe_rounds")
    op.drop_table("probe_node_state")
    op.drop_index("ix_probe_measurements_node_at", table_name="probe_measurements")
    op.drop_index("ix_probe_measurements_cycle", table_name="probe_measurements")
    op.drop_table("probe_measurements")
    op.drop_table("cf_records")
    op.drop_table("probe_sources")
    op.drop_table("probe_cycles")
    op.drop_index(
        "uq_cf_zones_one_active", table_name="cf_zones", postgresql_where=sa.text("pool_state = 'active'")
    )
    op.drop_table("cf_zones")
    op.drop_table("cf_state")
