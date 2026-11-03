"""Merge the parallel team heads (auth 0002, fleet 0003, agent 0004) into one line.

The three branches only create their own tables (no shared table is altered), so the merge has no
schema change of its own.

Revision ID: 0005_merge
Revises: 0002_auth, 0003_fleet, 0004_agent
"""

from __future__ import annotations

revision = "0005_merge"
down_revision = ("0002_auth", "0003_fleet", "0004_agent")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
