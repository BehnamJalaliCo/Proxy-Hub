"""Content engine tables (decision D51, docs/10 section 3.12, M3.1 phase A).

* ``content_sources``: the places the owner picks news from: a public Telegram channel (its
  public preview page ``https://t.me/s/<name>``, no Telegram account) or an RSS/Atom feed. Each
  has its own state: when it was last read, the last error (a short code; the dashboard shows it
  in Persian) and a back-off after errors. The owner adds, disables and removes them.
* ``content_candidates``: what a source gave, normalized to one shape (text, media reference, link,
  time, views), one row per post of a source (``source_id`` + ``external_id`` is unique, so a post
  is stored once). A candidate waits in the owner's review queue (decision D20) with a caption
  prefilled from the source text; the owner approves, edits or rejects it. An approved candidate
  becomes the content of the next post, before the old content bank. A candidate whose text (or
  photo) another source already gave is kept as ``duplicate`` of that one. Everything fetched is
  untrusted data: only plain text and checked https URLs are stored, never HTML.
* ``content_media``: a candidate's photo, video or video thumbnail, downloaded by the panel (photos
  and thumbnails when the candidate is fetched, a video only once the owner approved it). The
  dashboard shows them through the panel's own API, never from the source. Media is removed once
  the post went out or the candidate ended, so these rows stay few and short-lived.

Nothing here records anything about the people who read a channel (rule 7).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

SOURCE_KINDS = ("telegram", "rss")
CANDIDATE_STATUSES = ("pending", "approved", "rejected", "used", "expired", "duplicate")
MEDIA_KINDS = ("photo", "video")
MEDIA_STATES = ("none", "waiting", "ready", "failed")
MEDIA_ROLES = ("main", "thumb")
MEDIA_MIMES = ("image/jpeg", "image/png", "video/mp4")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class ContentSource(Base):
    __tablename__ = "content_sources"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_in(SOURCE_KINDS)})", name="kind"),
        CheckConstraint("length(ref) BETWEEN 1 AND 512", name="ref_length"),
        CheckConstraint("length(label) BETWEEN 1 AND 80", name="label_length"),
        CheckConstraint("failures >= 0 AND last_count >= 0", name="counts"),
        UniqueConstraint("kind", "ref", name="uq_content_sources_kind_ref"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # telegram: the channel's username (lower case, no @); rss: the feed's https URL.
    ref: Mapped[str] = mapped_column(String(512), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last try
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(40))  # a code; Persian in the dashboard
    failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # in a row
    next_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ContentCandidate(Base):
    __tablename__ = "content_candidates"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(CANDIDATE_STATUSES)})", name="status"),
        CheckConstraint(f"source_kind IN ({_in(SOURCE_KINDS)})", name="source_kind"),
        CheckConstraint(f"media_kind IS NULL OR media_kind IN ({_in(MEDIA_KINDS)})", name="media_kind"),
        CheckConstraint(f"media_state IN ({_in(MEDIA_STATES)})", name="media_state"),
        CheckConstraint("views IS NULL OR views >= 0", name="views"),
        CheckConstraint("length(title) <= 120 AND length(body) <= 2000", name="caption_length"),
        UniqueConstraint("source_id", "external_id", name="uq_content_candidates_source_external"),
        Index("ix_content_candidates_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content_sources.id", ondelete="SET NULL")
    )
    # Copied from the source, so a candidate still names it after the source was removed.
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_label: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1024))  # the original post (https only)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    views: Mapped[int | None] = mapped_column(BigInteger)
    source_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")  # plain text
    norm_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")  # for similarity
    title: Mapped[str] = mapped_column(String(120), nullable=False, server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")  # plain text
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="pending")
    duplicate_of: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content_candidates.id", ondelete="SET NULL")
    )
    media_kind: Mapped[str | None] = mapped_column(String(8))
    media_url: Mapped[str | None] = mapped_column(String(2048))  # never sent to the dashboard
    thumb_url: Mapped[str | None] = mapped_column(String(2048))
    media_state: Mapped[str] = mapped_column(String(8), nullable=False, server_default="none")
    media_error: Mapped[str | None] = mapped_column(String(40))
    media_tries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContentMedia(Base):
    __tablename__ = "content_media"
    __table_args__ = (
        CheckConstraint(f"role IN ({_in(MEDIA_ROLES)})", name="role"),
        CheckConstraint(f"mime IN ({_in(MEDIA_MIMES)})", name="mime"),
        CheckConstraint("size > 0 AND size = octet_length(data)", name="size"),
        UniqueConstraint("candidate_id", "role", name="uq_content_media_candidate_role"),
        Index("ix_content_media_sha256", "sha256"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content_candidates.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    mime: Mapped[str] = mapped_column(String(20), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)  # seconds, video only
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
