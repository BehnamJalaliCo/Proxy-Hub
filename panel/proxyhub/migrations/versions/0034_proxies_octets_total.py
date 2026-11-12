"""telemt_users.octets_total: a Telemt user's bytes across Telemt restarts

Telemt's per-user counter (``last_total_octets``, as the agent reports it) starts again from zero
whenever Telemt restarts (a port, TLS domain or mode change, a reboot, a crash). The panel adds
each report's growth here, or the whole new value after such a restart, as it does for Reality
usage; this is what the MTProto page shows as the proxy's total traffic. One number per proxy
secret, never per person (rule 7). Existing rows start from their last raw value.

Revision ID: 0034_proxies_octets_total
Revises: 0033_reality_traffic
Create Date: 2026-11-12 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_proxies_octets_total"
down_revision = "0033_reality_traffic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telemt_users", sa.Column("octets_total", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.execute("UPDATE telemt_users SET octets_total = COALESCE(last_total_octets, 0)")


def downgrade() -> None:
    op.drop_column("telemt_users", "octets_total")
