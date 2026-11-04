"""Telegram team tables: owner pairing codes, channel posts (edit-only record), per-chat pacing.

The bot token is never stored here: it lives in the vault (``secrets`` table, name
``telegram:bot``, AES-GCM). Non-secret Telegram state (bot id/username, owner chat id, channels,
no-post windows, update offset) is in ``settings`` under keys ``telegram.*``.

Nothing here records anything about people other than the owner: messages from anyone else
are ignored and not stored (CLAUDE.md rule 7, decision D4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

CHANNEL_ROLES = ("main", "test")


class TelegramPairing(Base):
    """A one-time code the owner sends to the bot (``/start <code>``). Only its SHA-256 is stored."""

    __tablename__ = "telegram_pairings"
    __table_args__ = (
        CheckConstraint("code_hash ~ '^[0-9a-f]{64}$'", name="code_hash_hex"),
        CheckConstraint("expires_at > created_at", name="window"),
        CheckConstraint("wrong_codes >= 0", name="wrong_codes"),
        Index(
            "ix_telegram_pairings_open",
            "expires_at",
            postgresql_where="used_at IS NULL AND cancelled_at IS NULL",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wrong_codes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ChannelPost(Base):
    """A message the bot posted in a channel. Posts are only ever edited, never removed (rule 6),
    and only through the message_id recorded here."""

    __tablename__ = "channel_posts"
    __table_args__ = (
        CheckConstraint("role IN ('main', 'test')", name="role"),
        CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="text_sha256_hex"),
        UniqueConstraint("chat_id", "message_id", name="uq_channel_posts_chat_message"),
        # One outbox job can produce at most one post (a send is never recorded twice).
        UniqueConstraint("outbox_id", name="uq_channel_posts_outbox_id"),
        Index("ix_channel_posts_role_created", "role", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outbox_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("outbox.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramPacing(Base):
    """Per-chat send pacing and Telegram flood-control waits (429 retry_after)."""

    __tablename__ = "telegram_pacing"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
