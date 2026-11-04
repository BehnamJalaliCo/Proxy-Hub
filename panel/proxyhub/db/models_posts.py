"""Posts team tables: the content library and what each automatic post carried.

* ``content_items``: short evergreen Persian texts (title + Telegram HTML body) that go on top of
  a post. The owner adds, edits, approves and retires them in the dashboard; nothing is ever
  removed from this table (an item is retired instead).
* ``posts``: one row per post the publisher queued (``outbox_id``). The content is kept as it was
  posted, so a later status edit re-renders the same text even if the item is edited meanwhile.
  Posts are never deleted, here or in the channel (CLAUDE.md rule 6).
* ``post_links``: the proxy links a post carries, by their stable key, label and status IN THIS
  POST. The link URL itself (it holds the proxy secret or the VLESS id) is NOT stored here: the
  outbox payload already holds the posted text, and an edit takes the current URL from its
  provider (proxyhub/links/registry.py). A status other than ``active`` never goes back to
  ``active`` in a post.

Nothing here records anything about the people who read the channel or use a proxy (rule 7).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

CONTENT_STATUSES = ("draft", "approved", "used", "retired")
CONTENT_ORIGINS = ("seed", "owner")
POST_ROLES = ("main", "test")
LINK_KINDS = ("mtproto", "reality")
LINK_STATUSES = ("active", "expired", "full", "burned")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(CONTENT_STATUSES)})", name="status"),
        CheckConstraint(f"origin IN ({_in(CONTENT_ORIGINS)})", name="origin"),
        CheckConstraint("used_count >= 0", name="used_count"),
        CheckConstraint("length(title) BETWEEN 1 AND 120", name="title_length"),
        CheckConstraint("length(body) BETWEEN 1 AND 3000", name="body_length"),
        Index("ix_content_items_status_id", "status", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="draft")
    origin: Mapped[str] = mapped_column(String(10), nullable=False, server_default="owner")
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Post(Base):
    """An automatic post. ``plan_day``/``plan_slot`` (Tehran date, slot index of that day's plan)
    are unique per role, so one slot can never be posted twice."""

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(f"role IN ({_in(POST_ROLES)})", name="role"),
        CheckConstraint("plan_slot >= 0", name="plan_slot"),
        CheckConstraint("edits >= 0 AND edit_failures >= 0", name="edit_counts"),
        UniqueConstraint("role", "plan_day", "plan_slot", name="uq_posts_role_plan"),
        UniqueConstraint("outbox_id", name="uq_posts_outbox_id"),
        Index("ix_posts_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    outbox_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("outbox.id", ondelete="SET NULL"))
    content_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content_items.id", ondelete="SET NULL")
    )
    content_html: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    channel_tag: Mapped[str] = mapped_column(String(40), nullable=False)
    poster: Mapped[str] = mapped_column(String(20), nullable=False, server_default="bot")
    plan_day: Mapped[date] = mapped_column(Date, nullable=False)
    plan_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_edit_outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outbox.id", ondelete="SET NULL")
    )
    last_edit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    edit_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    edit_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PostLink(Base):
    __tablename__ = "post_links"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_in(LINK_KINDS)})", name="kind"),
        CheckConstraint(f"status IN ({_in(LINK_STATUSES)})", name="status"),
        CheckConstraint("position >= 0", name="position"),
        UniqueConstraint("post_id", "key", name="uq_post_links_post_key"),
        UniqueConstraint("post_id", "position", name="uq_post_links_post_position"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    slot: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
