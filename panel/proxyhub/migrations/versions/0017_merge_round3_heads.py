"""Merge the round-3 team heads into one line.

Each team branch starts at 0010_domains and only creates its own tables (no shared table is
altered), so the merge has no schema change of its own.

Revision ID: 0017_merge
Revises: 0011_mtproto, 0012_sponsor, 0016_analytics
"""

from __future__ import annotations

revision = "0017_merge"
down_revision = ("0011_mtproto", "0012_sponsor", "0016_analytics")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
