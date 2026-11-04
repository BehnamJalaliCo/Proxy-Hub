"""domains: automatic domain pool, filter checks from Iran, deletion of filtered zones (D35)

* ``cf_zones``: when a zone was filtered or deleted, and the state of its deletion from Cloudflare.
  ``pool_state`` gains ``deleted`` (a terminal state: the zone no longer exists in Cloudflare).
* ``cf_state.standby_alert_at``: the last "no standby domain left" reminder to the owner.
* ``domain_checks``: one row per filter check of a domain from Iran (counts per network only).

Revision ID: 0010_domains
Revises: 0009_merge
Create Date: 2026-11-04 00:30:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_domains"
down_revision = "0009_merge"
branch_labels = None
depends_on = None

OLD_POOL = "pool_state IN ('none', 'active', 'standby', 'filtered', 'retired')"
NEW_POOL = "pool_state IN ('none', 'active', 'standby', 'filtered', 'retired', 'deleted')"


def upgrade() -> None:
    op.add_column("cf_state", sa.Column("standby_alert_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cf_zones", sa.Column("filtered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cf_zones", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cf_zones", sa.Column("delete_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cf_zones", sa.Column("delete_blocked", sa.String(length=60), nullable=True))
    op.add_column("cf_zones", sa.Column("delete_error", sa.String(length=30), nullable=True))
    op.add_column(
        "cf_zones", sa.Column("delete_attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("cf_zones", sa.Column("delete_retry_at", sa.DateTime(timezone=True), nullable=True))
    # A zone the owner marked filtered before this revision starts its grace period now.
    op.execute("UPDATE cf_zones SET filtered_at = now() WHERE pool_state = 'filtered'")
    op.drop_constraint(op.f("ck_cf_zones_pool_state"), "cf_zones", type_="check")
    op.create_check_constraint(op.f("ck_cf_zones_pool_state"), "cf_zones", NEW_POOL)

    op.create_table(
        "domain_checks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("zone_id", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pool_state", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=True),
        sa.Column("probes_asked", sa.Integer(), nullable=False),
        sa.Column("probes", sa.Integer(), nullable=False),
        sa.Column("answered", sa.Integer(), nullable=False),
        sa.Column("hijacked", sa.Integer(), nullable=False),
        sa.Column("networks_answered", sa.Integer(), nullable=False),
        sa.Column("networks_hijacked", sa.Integer(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("verdict_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "probes_asked >= 0 AND probes >= 0 AND answered >= 0 AND hijacked >= 0 AND hijacked <= answered"
            " AND networks_answered >= 0 AND networks_hijacked >= 0 AND networks_hijacked <= networks_answered",
            name=op.f("ck_domain_checks_counts"),
        ),
        sa.CheckConstraint(NEW_POOL, name=op.f("ck_domain_checks_pool_state")),
        sa.CheckConstraint(
            "status IN ('ok', 'no_probes', 'rate_limited', 'error', 'timeout')",
            name=op.f("ck_domain_checks_status"),
        ),
        sa.CheckConstraint(
            "verdict IN ('filtered', 'clear', 'unknown')", name=op.f("ck_domain_checks_verdict")
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["cf_zones.id"], name=op.f("fk_domain_checks_zone_id_cf_zones"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain_checks")),
    )
    op.create_index("ix_domain_checks_zone_at", "domain_checks", ["zone_id", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_domain_checks_zone_at", table_name="domain_checks")
    op.drop_table("domain_checks")
    # A deleted zone no longer exists in Cloudflare; the older schema has no word for it.
    op.execute("UPDATE cf_zones SET pool_state = 'retired' WHERE pool_state = 'deleted'")
    op.drop_constraint(op.f("ck_cf_zones_pool_state"), "cf_zones", type_="check")
    op.create_check_constraint(op.f("ck_cf_zones_pool_state"), "cf_zones", OLD_POOL)
    op.drop_column("cf_zones", "delete_retry_at")
    op.drop_column("cf_zones", "delete_attempts")
    op.drop_column("cf_zones", "delete_error")
    op.drop_column("cf_zones", "delete_blocked")
    op.drop_column("cf_zones", "delete_claimed_at")
    op.drop_column("cf_zones", "deleted_at")
    op.drop_column("cf_zones", "filtered_at")
    op.drop_column("cf_state", "standby_alert_at")
