"""xhttp: VLESS + XHTTP + REALITY on its own port next to every REALITY inbound (agent 0.4.0)

* ``reality_keys.xhttp_path``: the scope's XHTTP path ("/" + 16 random hex), kept with the key so a
  slot's XHTTP links keep their path on a new server. The database makes it for every key (the
  column default), so existing keys get one here and new keys without any code change.
* ``reality_node_status.xhttp_port``: the port the node's agent says its XHTTP inbound runs on
  (NULL: none, or an agent before 0.4.0). An XHTTP link is published only for a node that says so.

Revision ID: 0019_xhttp
Revises: 0018_wstls
Create Date: 2026-11-05 18:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_xhttp"
down_revision = "0018_wstls"
branch_labels = None
depends_on = None

# 64 random bits from PostgreSQL's strong random source (gen_random_uuid, built in since PG 13), as
# for ws_path in 0018: the first 16 hex digits of a version-4 UUID hold 60 random bits.
RANDOM_PATH = "'/' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 16)"


def upgrade() -> None:
    op.add_column(
        "reality_keys",
        sa.Column("xhttp_path", sa.String(length=17), server_default=sa.text(RANDOM_PATH), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_reality_keys_xhttp_path"), "reality_keys", "xhttp_path ~ '^/[0-9a-f]{16}$'"
    )
    op.add_column("reality_node_status", sa.Column("xhttp_port", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_reality_node_status_xhttp_port"),
        "reality_node_status",
        "xhttp_port IS NULL OR xhttp_port BETWEEN 1 AND 65535",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_reality_node_status_xhttp_port"), "reality_node_status", type_="check")
    op.drop_column("reality_node_status", "xhttp_port")
    op.drop_constraint(op.f("ck_reality_keys_xhttp_path"), "reality_keys", type_="check")
    op.drop_column("reality_keys", "xhttp_path")
