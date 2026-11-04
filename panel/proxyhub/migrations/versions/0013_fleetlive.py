"""fleetlive: the live 9 + 1 fleet (D14) and lost domain delegations

* ``ip_history``: every public IPv4 our servers got, and every burned one with its /24.
* ``fleet_alerts``: the live fleet's owner alerts (a cap blocks a needed action), repeated rarely.
* ``node_operations.retire_reason`` gains the fleet's reasons (burned, unhealthy, duplicate,
  excess, burned_ip).
* ``domain_delegation_checks`` / ``domain_delegation_state``: hourly DNS-over-HTTPS NS lookups of
  the pool domains (is each still registered and delegated to Cloudflare?).

Revision ID: 0013_fleetlive
Revises: 0010_domains
Create Date: 2026-11-04 20:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_fleetlive"
down_revision = "0010_domains"
branch_labels = None
depends_on = None

OLD_RETIRE = "retire_reason IS NULL OR retire_reason IN ('owner', 'unhealthy_test_node', 'create_failed')"
NEW_RETIRE = (
    "retire_reason IS NULL OR retire_reason IN ('owner', 'unhealthy_test_node', 'create_failed', "
    "'burned', 'unhealthy', 'duplicate', 'excess', 'burned_ip')"
)
RESULTS = "result IN ('ok', 'nxdomain', 'not_cloudflare', 'ns_changed', 'no_answer', 'servfail', 'error')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", type_="check")
    op.create_check_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", NEW_RETIRE)

    op.create_table(
        "ip_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("prefix24", postgresql.CIDR(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("location", sa.String(length=20), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("burned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("burned_reason", sa.String(length=30), nullable=True),
        sa.CheckConstraint("family(ip) = 4 AND masklen(ip) = 32", name=op.f("ck_ip_history_ipv4_host")),
        sa.CheckConstraint("prefix24 = network(set_masklen(ip, 24))", name=op.f("ck_ip_history_prefix24")),
        sa.CheckConstraint("(burned_at IS NULL) = (burned_reason IS NULL)", name=op.f("ck_ip_history_burn_pair")),
        sa.CheckConstraint(
            "burned_reason IS NULL OR burned_reason IN ('probe_blocked')", name=op.f("ck_ip_history_burn_reason")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["hetzner_accounts.id"],
            name=op.f("fk_ip_history_account_id_hetzner_accounts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_ip_history_node_id_nodes"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ip_history")),
        sa.UniqueConstraint("node_id", "ip", name="uq_ip_history_node_ip"),
    )
    op.create_index("ix_ip_history_ip", "ip_history", ["ip"], unique=False)
    op.create_index(
        "ix_ip_history_prefix24_burned",
        "ip_history",
        ["prefix24"],
        unique=False,
        postgresql_where=sa.text("burned_at IS NOT NULL"),
    )

    op.create_table(
        "fleet_alerts",
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_fleet_alerts")),
    )

    op.create_table(
        "domain_delegation_checks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("zone_id", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pool_state", sa.String(length=10), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("rcode", sa.SmallInteger(), nullable=True),
        sa.Column("nameservers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.CheckConstraint(RESULTS, name=op.f("ck_domain_delegation_checks_result")),
        sa.CheckConstraint(
            "verdict IN ('ok', 'lost', 'unknown')", name=op.f("ck_domain_delegation_checks_verdict")
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["cf_zones.id"],
            name=op.f("fk_domain_delegation_checks_zone_id_cf_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain_delegation_checks")),
    )
    op.create_index(
        "ix_domain_delegation_checks_zone_at", "domain_delegation_checks", ["zone_id", "at"], unique=False
    )

    op.create_table(
        "domain_delegation_state",
        sa.Column("zone_id", sa.String(length=32), nullable=False),
        sa.Column("baseline_ns", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("baseline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_from", sa.String(length=10), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["cf_zones.id"],
            name=op.f("fk_domain_delegation_state_zone_id_cf_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("zone_id", name=op.f("pk_domain_delegation_state")),
    )


def downgrade() -> None:
    op.drop_table("domain_delegation_state")
    op.drop_index("ix_domain_delegation_checks_zone_at", table_name="domain_delegation_checks")
    op.drop_table("domain_delegation_checks")
    op.drop_table("fleet_alerts")
    op.drop_index("ix_ip_history_prefix24_burned", table_name="ip_history", postgresql_where=sa.text("burned_at IS NOT NULL"))
    op.drop_index("ix_ip_history_ip", table_name="ip_history")
    op.drop_table("ip_history")
    # Rows with a round-3 reason cannot keep it under the old check: map them to the closest old one.
    op.execute(
        "UPDATE node_operations SET retire_reason = 'create_failed' "
        "WHERE retire_reason IN ('burned', 'unhealthy', 'duplicate', 'excess', 'burned_ip')"
    )
    op.drop_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", type_="check")
    op.create_check_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", OLD_RETIRE)
