"""iranprobe: TLS handshakes from Iran decide (P15), location scores, automatic SNI targets

* ``probe_measurements.kind`` may be ``https`` (Globalping ``http``: a real TLS handshake; MCI and
  Rightel answer a TCP SYN themselves for blocked addresses, so TCP decides nothing any more), and
  ``role`` may be ``target`` (a candidate REALITY / fake-TLS site measured without a node).
* ``probe_sources.tls_probes``: how many probes the last TLS control used (budget planning).
* ``probe_location_reach``: per node and network, the latest valid TLS outcome, with the node's
  Hetzner location (the fleet prefers locations open on MCI and Rightel; proxyhub/fleet/locations.py).
* ``target_states``: per candidate site, the last two rounds from Iran and our nodes' own check.

Revision ID: 0019_iranprobe
Revises: 0018_wstls
Create Date: 2026-11-05 18:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019_iranprobe"
down_revision = "0018_wstls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_probe_measurements_kind"), "probe_measurements", type_="check")
    op.create_check_constraint(
        op.f("ck_probe_measurements_kind"), "probe_measurements", "kind IN ('tcp', 'icmp', 'dns', 'https')"
    )
    op.drop_constraint(op.f("ck_probe_measurements_role"), "probe_measurements", type_="check")
    op.create_check_constraint(
        op.f("ck_probe_measurements_role"), "probe_measurements", "role IN ('node', 'control', 'target')"
    )
    op.add_column("probe_sources", sa.Column("tls_probes", sa.Integer(), nullable=True))

    op.create_table(
        "probe_location_reach",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("network_key", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=100), nullable=False),
        sa.Column("asn", sa.BigInteger(), nullable=True),
        sa.Column("location", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('open', 'closed')", name=op.f("ck_probe_location_reach_state")),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_probe_location_reach_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("node_id", "network_key", name=op.f("pk_probe_location_reach")),
    )
    op.create_index(
        "ix_probe_location_reach_location_at", "probe_location_reach", ["location", "at"], unique=False
    )

    op.create_table(
        "target_states",
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("iran", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("iran_prev", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("node_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("node_fail_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("host", name=op.f("pk_target_states")),
    )


def downgrade() -> None:
    op.drop_table("target_states")
    op.drop_index("ix_probe_location_reach_location_at", table_name="probe_location_reach")
    op.drop_table("probe_location_reach")
    op.drop_column("probe_sources", "tls_probes")
    op.execute("DELETE FROM probe_measurements WHERE kind = 'https' OR role = 'target'")
    op.drop_constraint(op.f("ck_probe_measurements_role"), "probe_measurements", type_="check")
    op.create_check_constraint(op.f("ck_probe_measurements_role"), "probe_measurements", "role IN ('node', 'control')")
    op.drop_constraint(op.f("ck_probe_measurements_kind"), "probe_measurements", type_="check")
    op.create_check_constraint(
        op.f("ck_probe_measurements_kind"), "probe_measurements", "kind IN ('tcp', 'icmp', 'dns')"
    )
