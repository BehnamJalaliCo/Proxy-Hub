"""The sponsor automation's own bookkeeping (docs/11 section 2, rows 1, 3, 4, 5 and 7).

The registrations themselves live in ``mtproxy_registrations`` and the paid campaigns in
``sponsor_campaigns`` (proxyhub/db/models.py). ``sponsor_flows`` adds, per registration, what the
reconciler needs to drive @MTProxybot: which live proxy uses the registration now, how far the last
conversation got, retries, and why it stopped. Whether a registration still needs its tag from
@MTProxybot or only its promotion follows from ``mtproxy_registrations.ad_tag`` (NULL: no tag yet).
Nothing here is secret: no proxy secret, and the bot's last reply is stored only with every secret
masked (proxyhub/sponsor/replies.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base


class SponsorFlow(Base):
    __tablename__ = "sponsor_flows"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts"),
        Index("ix_sponsor_flows_proxy_key", "proxy_key"),
    )
    registration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mtproxy_registrations.id", ondelete="CASCADE"), primary_key=True
    )
    # The live proxy using this registration now: the provider's stable key
    # ("mtproto:<node_id>:<user>", docs cross-team contract).
    proxy_key: Mapped[str | None] = mapped_column(String(160))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(60))
    last_step: Mapped[str | None] = mapped_column(String(24))
    stuck_reason: Mapped[str | None] = mapped_column(String(60))
    stuck_hint: Mapped[str | None] = mapped_column(String(30))
    stuck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bot_text: Mapped[str | None] = mapped_column(Text)  # masked; only while stuck
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promotion_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
