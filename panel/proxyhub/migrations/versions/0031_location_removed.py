"""a slot moves off a location the owner removed (decision D60)

``node_operations.retire_reason`` may now be ``location_removed``: the fleet planner retires a node
whose location is no longer in the owner's list once its replacement has settled (or first, when
every Hetzner account is full; proxyhub/fleet/planner.py).

Downgrade: the constraint goes back; a row that already carries the new reason must be gone first
(it is refused otherwise), so nothing is silently rewritten.

Revision ID: 0031_location_removed
Revises: 0030_retire_seed_content
Create Date: 2026-11-07 20:30:00
"""

from alembic import op

revision = "0031_location_removed"
down_revision = "0030_retire_seed_content"
branch_labels = None
depends_on = None

OLD = (
    "retire_reason IS NULL OR retire_reason IN ('owner', 'unhealthy_test_node', 'create_failed', "
    "'burned', 'unhealthy', 'duplicate', 'excess', 'burned_ip')"
)
NEW = (
    "retire_reason IS NULL OR retire_reason IN ('owner', 'unhealthy_test_node', 'create_failed', "
    "'burned', 'unhealthy', 'duplicate', 'excess', 'burned_ip', 'location_removed')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", type_="check")
    op.create_check_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", NEW)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", type_="check")
    op.create_check_constraint(op.f("ck_node_operations_retire_reason"), "node_operations", OLD)
