"""post gone: remember a post whose message was deleted in the channel by hand

* ``posts.gone_at``: set when an edit of the post found its message gone (the bot's "message to edit
  not found" / ``MESSAGE_ID_INVALID``, the operator account's ``MESSAGE_ID_INVALID``). The publisher
  never edits such a post again and does not alert the owner; the dashboard shows it as gone. The
  panel itself still never deletes a post (CLAUDE.md rule 6).

Downgrade drops the column: a gone post is then edited again (and fails) as before.

Revision ID: 0024_post_gone
Revises: 0023_posting_compact
Create Date: 2026-11-06 18:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0024_post_gone"
down_revision = "0023_posting_compact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("gone_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "gone_at")
