"""retire the seeded content bank (the owner, 2026-11-07, decision D58)

The 36 evergreen items seeded by 0014_posts (Telegram tips, phone, privacy) were the publisher's
fallback when no source candidate was approved for a slot. The owner judged them not good enough
for the main channel ("جالب نیستند"); the sources' news is what he wants. Every seeded item becomes
``retired``, whatever its status (a ``used`` one would otherwise come back after
``recycle_after_days``). Items the owner wrote (``origin = 'owner'``) are left as they are. Nothing
is deleted: the owner can approve a seeded item again on the Content page.

Downgrade changes nothing: which seeded items were approved or used before is not kept, and a
retired item is harmless.

Revision ID: 0030_retire_seed_content
Revises: 0029_breaking_news
Create Date: 2026-11-07 16:00:00
"""

from alembic import op

revision = "0030_retire_seed_content"
down_revision = "0029_breaking_news"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE content_items SET status = 'retired', updated_at = now() "
        "WHERE origin = 'seed' AND status <> 'retired'"
    )


def downgrade() -> None:
    pass
