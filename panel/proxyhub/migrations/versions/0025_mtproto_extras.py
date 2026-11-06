"""mtproto extras: more public links per node (decision D49)

* ``mtproto_proxies`` may now hold several rows per node: ``ordinal`` 0 is the node's primary user
  (its registered public proxy, as before; every existing row becomes ordinal 0), ``ordinal`` 1 to
  5 are EXTRA Telemt users the policy adds so that a post can carry six links while fewer nodes run.
  The primary key becomes (``node_id``, ``user_name``); one row per (node, ordinal).

Downgrade: the extra rows are forgotten here (ordinal > 0). Their Telemt users stay in
``telemt_users``, so links already posted keep working on their nodes; the older code simply no
longer publishes them in new posts.

Revision ID: 0025_mtproto_extras
Revises: 0024_post_gone
Create Date: 2026-11-06 20:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_mtproto_extras"
down_revision = "0024_post_gone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mtproto_proxies", sa.Column("ordinal", sa.SmallInteger(), server_default="0", nullable=False)
    )
    op.drop_constraint(op.f("pk_mtproto_proxies"), "mtproto_proxies", type_="primary")
    op.create_primary_key(op.f("pk_mtproto_proxies"), "mtproto_proxies", ["node_id", "user_name"])
    op.create_unique_constraint("uq_mtproto_proxies_node_ordinal", "mtproto_proxies", ["node_id", "ordinal"])
    op.create_check_constraint(op.f("ck_mtproto_proxies_ordinal"), "mtproto_proxies", "ordinal >= 0 AND ordinal < 6")


def downgrade() -> None:
    op.execute("DELETE FROM mtproto_proxies WHERE ordinal > 0")
    op.drop_constraint(op.f("ck_mtproto_proxies_ordinal"), "mtproto_proxies", type_="check")
    op.drop_constraint("uq_mtproto_proxies_node_ordinal", "mtproto_proxies", type_="unique")
    op.drop_constraint(op.f("pk_mtproto_proxies"), "mtproto_proxies", type_="primary")
    op.create_primary_key(op.f("pk_mtproto_proxies"), "mtproto_proxies", ["node_id"])
    op.drop_column("mtproto_proxies", "ordinal")
