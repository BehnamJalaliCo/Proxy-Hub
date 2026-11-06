"""post link operator: the operator a post names an MTProto link after (decision D50)

* ``post_links.operator``: ``mci``, ``rightel`` or ``irancell`` when the post shows the link as that
  operator's name («همراه اول», «رایتل», «ایرانسل»), decided once when the post was made from the
  panel's own fresh TLS probes from that operator's network on the link's port; NULL for the plain
  word «پروکسی» (every post made before D50 keeps NULL). Status edits never change it.

Downgrade drops the column: the posts' next status edit then shows «پروکسی» again.

Revision ID: 0026_post_link_operator
Revises: 0025_mtproto_extras
Create Date: 2026-11-06 20:30:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0026_post_link_operator"
down_revision = "0025_mtproto_extras"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("post_links", sa.Column("operator", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_post_links_operator"),
        "post_links",
        "operator IS NULL OR operator IN ('mci', 'rightel', 'irancell')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_post_links_operator"), "post_links", type_="check")
    op.drop_column("post_links", "operator")
