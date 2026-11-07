"""breaking news (decision D56): a fresh story several sources carry, posted at once

* ``content_candidates.qc_breaking``: Claude's hint in its review that the item is breaking news.
* ``content_candidates.breaking_at``: when the content supervisor found an approved candidate to be
  breaking news (Claude said so AND the deterministic gates held: young, carried by several
  sources); the publisher posts it out of the day plan.
* ``posts.breaking``: the post was such an out-of-plan breaking-news post. It does not count
  against ``posts_per_day``; its ``plan_slot`` is :data:`BREAKING_SLOT_BASE` + n.

Downgrade drops both columns; breaking posts already made stay as ordinary posts.

Revision ID: 0029_breaking_news
Revises: 0028_content_qc
Create Date: 2026-11-07 09:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0029_breaking_news"
down_revision = "0028_content_qc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_candidates", sa.Column("qc_breaking", sa.Boolean(), nullable=True))
    op.add_column("content_candidates", sa.Column("breaking_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("posts", sa.Column("breaking", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("posts", "breaking")
    op.drop_column("content_candidates", "breaking_at")
    op.drop_column("content_candidates", "qc_breaking")
