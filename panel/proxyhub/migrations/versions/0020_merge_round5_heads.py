"""Merge the round-5 heads: XHTTP (0019_xhttp) and the Iran TLS probes (0019_iranprobe).

Both start at 0018_wstls and touch different tables and columns, so the merge has no schema change
of its own.

Revision ID: 0020_merge
Revises: 0019_xhttp, 0019_iranprobe
"""

from __future__ import annotations

revision = "0020_merge"
down_revision = ("0019_xhttp", "0019_iranprobe")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
