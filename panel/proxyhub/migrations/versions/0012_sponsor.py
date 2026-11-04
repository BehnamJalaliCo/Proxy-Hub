"""sponsor: the @MTProxybot automation's bookkeeping per registration (docs/11, D25, D37)

* ``sponsor_flows``: one row per ``mtproxy_registrations`` row: the live proxy using it now, retries,
  the step the last conversation reached, and why it stopped (with the bot's last reply, every
  proxy secret masked). No secret is stored.

The registrations, campaigns and stats tables already exist (0001); their columns are unchanged.

Revision ID: 0012_sponsor
Revises: 0010_domains
Create Date: 2026-11-04 12:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_sponsor"
down_revision = "0010_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sponsor_flows",
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proxy_key", sa.String(length=160), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=60), nullable=True),
        sa.Column("last_step", sa.String(length=24), nullable=True),
        sa.Column("stuck_reason", sa.String(length=60), nullable=True),
        sa.Column("stuck_hint", sa.String(length=30), nullable=True),
        sa.Column("stuck_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_bot_text", sa.Text(), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promotion_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_sponsor_flows_attempts")),
        sa.ForeignKeyConstraint(
            ["registration_id"],
            ["mtproxy_registrations.id"],
            name=op.f("fk_sponsor_flows_registration_id_mtproxy_registrations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("registration_id", name=op.f("pk_sponsor_flows")),
    )
    op.create_index("ix_sponsor_flows_proxy_key", "sponsor_flows", ["proxy_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sponsor_flows_proxy_key", table_name="sponsor_flows")
    op.drop_table("sponsor_flows")
