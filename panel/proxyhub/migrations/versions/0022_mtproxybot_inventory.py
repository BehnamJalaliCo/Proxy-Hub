"""mtproxybot inventory: @MTProxybot's own list of our proxies (docs/11, D25, D37)

* ``mtproxybot_entries``: one row per button of the operator account's /myproxies list as it was
  last read: the address the button names, the label as a shape and a one-way fingerprint (never
  the label's hex, a secret or a tag), the registration whose tag the label names (if any), the
  Proxy Sponsor the proxy's own screen names, and the cleanup's progress (observed, removed,
  unconfirmed, stuck, interrupted). The job and its settings live in the ``settings`` row
  ``sponsor.mtproxybot``.

Revision ID: 0022_mtproxybot_inventory
Revises: 0021_operator_poster
Create Date: 2026-11-05 21:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022_mtproxybot_inventory"
down_revision = "0021_operator_poster"
branch_labels = None
depends_on = None

REMOVAL_STATES = "'observed', 'removed', 'unconfirmed', 'stuck', 'interrupted'"


def upgrade() -> None:
    op.create_table(
        "mtproxybot_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("label_shape", sa.String(length=200), nullable=False),
        sa.Column("label_fp", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("tag_registration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("page", sa.SmallInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("pressable", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("menu_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("menu_channel", sa.String(length=32), nullable=True),
        sa.Column("menu_note", sa.String(length=60), nullable=True),
        sa.Column("removal_state", sa.String(length=16), nullable=True),
        sa.Column("removal_reason", sa.String(length=60), nullable=True),
        sa.Column("removal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "port IS NULL OR (port >= 1 AND port <= 65535)", name=op.f("ck_mtproxybot_entries_port")
        ),
        sa.CheckConstraint(
            f"removal_state IS NULL OR removal_state IN ({REMOVAL_STATES})",
            name=op.f("ck_mtproxybot_entries_removal_state"),
        ),
        sa.ForeignKeyConstraint(
            ["tag_registration_id"],
            ["mtproxy_registrations.id"],
            name=op.f("fk_mtproxybot_entries_tag_registration_id_mtproxy_registrations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mtproxybot_entries")),
        sa.UniqueConstraint("label_fp", "ordinal", name="uq_mtproxybot_entries_label"),
    )


def downgrade() -> None:
    op.drop_table("mtproxybot_entries")
    op.execute("DELETE FROM settings WHERE key = 'sponsor.mtproxybot'")
