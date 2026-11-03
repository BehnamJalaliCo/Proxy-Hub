"""auth: admin login (M1.3)

Owner account, one-time setup links, TOTP enrollments, server-side sessions, recovery codes and
brute-force throttle counters. No secret is stored in clear and no client IP is stored.

Revision ID: 0002_auth
Revises: 0001
Create Date: 2026-11-03 13:23:50.929482
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_auth"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_setup_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_setup_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_setup_tokens_token_hash")),
    )
    op.create_table(
        "admin_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("totp_last_step", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("username ~ '^[a-z0-9_.-]{3,32}$'", name=op.f("ck_admin_users_username_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint("username", name=op.f("uq_admin_users_username")),
    )
    op.create_table(
        "auth_throttle",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_level", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lock_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_auth_throttle")),
    )
    op.create_table(
        "admin_enrollments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("setup_token_id", sa.UUID(), nullable=False),
        sa.Column("totp_secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["setup_token_id"],
            ["admin_setup_tokens.id"],
            name=op.f("fk_admin_enrollments_setup_token_id_admin_setup_tokens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_enrollments")),
    )
    op.create_index(
        op.f("ix_admin_enrollments_setup_token_id"), "admin_enrollments", ["setup_token_id"], unique=False
    )
    op.create_table(
        "admin_recovery_codes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_recovery_codes_user_id_admin_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_recovery_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_admin_recovery_codes_code_hash")),
    )
    op.create_index(op.f("ix_admin_recovery_codes_user_id"), "admin_recovery_codes", ["user_id"], unique=False)
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["admin_users.id"], name=op.f("fk_admin_sessions_user_id_admin_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_admin_sessions_token_hash")),
    )
    op.create_index(op.f("ix_admin_sessions_user_id"), "admin_sessions", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_sessions_user_id"), table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_index(op.f("ix_admin_recovery_codes_user_id"), table_name="admin_recovery_codes")
    op.drop_table("admin_recovery_codes")
    op.drop_index(op.f("ix_admin_enrollments_setup_token_id"), table_name="admin_enrollments")
    op.drop_table("admin_enrollments")
    op.drop_table("auth_throttle")
    op.drop_table("admin_users")
    op.drop_table("admin_setup_tokens")
