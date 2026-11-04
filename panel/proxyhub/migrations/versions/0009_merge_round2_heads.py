"""Merge the round-2 team heads (telegram 0006, live 0007, net 0008) into one line.

Each branch only creates its own tables (no shared table is altered), so the merge has no schema
change of its own.

Revision ID: 0009_merge
Revises: 0006_tg, 0007_live, 0008_net
"""

from __future__ import annotations

revision = "0009_merge"
down_revision = ("0006_tg", "0007_live", "0008_net")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
