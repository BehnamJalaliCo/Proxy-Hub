"""Auth tables (M1.3): the owner's admin account, setup links, sessions, recovery codes, throttle.

Nothing here holds a secret in clear (CLAUDE.md rule 3): tokens and codes are SHA-256 digests, the
TOTP secret is AES-256-GCM encrypted (proxyhub/auth/secretbox.py) and the password is argon2id.
Nothing here holds a client IP (rule 7); sessions keep only a shortened User-Agent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AdminUser(Base):
    __tablename__ = "admin_users"
    __table_args__ = (CheckConstraint("username ~ '^[a-z0-9_.-]{3,32}$'", name="username_format"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Last TOTP time step accepted for this account: codes of this step or older are refused.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminSetupToken(Base):
    """One-time setup link (``python -m proxyhub setup-link``). Only the SHA-256 is stored."""

    __tablename__ = "admin_setup_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminEnrollment(Base):
    """A pending TOTP secret handed out by /api/setup/begin, tied to its setup token."""

    __tablename__ = "admin_enrollments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    setup_token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_setup_tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    totp_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now_col()


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(160))


class AdminRecoveryCode(Base):
    __tablename__ = "admin_recovery_codes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now_col()
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthThrottle(Base):
    """Failure counters for brute-force protection: one row per username (keyed by an HMAC of
    the name, so a password typed into the username box is never stored) and one 'global' row."""

    __tablename__ = "auth_throttle"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_lock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
