"""when an account's server limit was learned

``hetzner_accounts.learned_server_limit_at``: set together with ``learned_server_limit`` when Hetzner
refuses a create (proxyhub/accounts/limits.py). The fleet planner treats a limit older than its
``LIMIT_RECHECK`` (or one with no time, as every limit learned before this migration) as unknown and
tries again, so a limit Hetzner raised is used without anyone's action (the owner, 2026-11-08: he
asks Hetzner for 9 servers; the panel had learned 4 and would never have tried a fifth).

Revision ID: 0032_limit_learned_at
Revises: 0031_location_removed
Create Date: 2026-11-08 01:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_limit_learned_at"
down_revision = "0031_location_removed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hetzner_accounts", sa.Column("learned_server_limit_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("hetzner_accounts", "learned_server_limit_at")
