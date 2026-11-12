"""auth: the owner's known browsers (device cookies)

``admin_devices``: one row per browser the owner logged in from, holding only the SHA-256 of its
``ph_device`` cookie. A login from such a browser is throttled on its own counter, so strangers
who keep the global or the username lock tripped cannot shut the owner out. No IP, no device name.

Revision ID: 0034_api_admin_devices
Revises: 0033_reality_traffic
Create Date: 2026-11-12 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_api_admin_devices"
down_revision = "0033_reality_traffic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_devices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["admin_users.id"], name=op.f("fk_admin_devices_user_id_admin_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_devices")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_devices_token_hash")),
    )
    op.create_index(op.f("ix_admin_devices_user_id"), "admin_devices", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_devices_user_id"), table_name="admin_devices")
    op.drop_table("admin_devices")
