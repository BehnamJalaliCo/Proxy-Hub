"""posting compact: one Reality pair and six MTProto links per post (decision D48)

The owner (2026-11-06) shortened the link part of a post, so news can sit above it: one Reality
config (with its XHTTP link) and six MTProto links. The defaults in proxyhub/posts/settings.py
changed with this revision; an owner who already saved the posting settings has them stored in the
``settings`` row ``posting`` (JSON), and that row takes ``max_reality = 1`` and ``max_mtproto = 6``
here, every other key as it was. Without the row (never saved) the new defaults apply by themselves.

Downgrade: a value that still is the D48 one goes back to the old default (``max_reality = 3``,
``max_mtproto = 12``); a value the owner changed since is left alone. Each key on its own.

Revision ID: 0023_posting_compact
Revises: 0022_mtproxybot_inventory
Create Date: 2026-11-06 12:00:00
"""

from alembic import op

revision = "0023_posting_compact"
down_revision = "0022_mtproxybot_inventory"
branch_labels = None
depends_on = None

# (key, old default, D48 value)
CHANGED = (("max_reality", 3, 1), ("max_mtproto", 12, 6))


def upgrade() -> None:
    new = ", ".join(f'"{key}": {value}' for key, _, value in CHANGED)
    op.execute(
        f"UPDATE settings SET value = value || '{{{new}}}'::jsonb, updated_at = now() "
        "WHERE key = 'posting' AND jsonb_typeof(value) = 'object'"
    )


def downgrade() -> None:
    for key, old, value in CHANGED:
        op.execute(
            f"UPDATE settings SET value = jsonb_set(value, '{{{key}}}', '{old}'::jsonb), "
            "updated_at = now() WHERE key = 'posting' AND jsonb_typeof(value) = 'object' "
            f"AND value -> '{key}' = '{value}'::jsonb"
        )
