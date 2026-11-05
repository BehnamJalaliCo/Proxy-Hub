"""The sponsor automation's own bookkeeping (docs/11 section 2, rows 1, 3, 4, 5 and 7).

The registrations themselves live in ``mtproxy_registrations`` and the paid campaigns in
``sponsor_campaigns`` (proxyhub/db/models.py). A row of ``mtproxy_registrations`` is written only
once @MTProxybot gave the proxy its tag: until then the proxy counts as "without a registration"
for team mtproto's ``proxies_to_register`` (cross-team SPONSOR contract), which is the only place
its secret comes from.

``sponsor_flows`` is one row per proxy the module works on, from the first time the provider lists
it: its address and Telemt user (copied from the registration once there is one), the live proxy
that uses it now, how far the last conversation got, retries, why it stopped, and whether the tag
reached the node's desired state. ``registration_id`` is NULL while the proxy has no tag yet (a
"candidate"). Nothing here is secret: no proxy secret, and the bot's last reply is stored only with
every secret masked (proxyhub/sponsor/replies.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

REMOVAL_STATES = ("observed", "removed", "unconfirmed", "stuck", "interrupted")


class SponsorFlow(Base):
    __tablename__ = "sponsor_flows"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint("port >= 1 AND port <= 65535", name="port"),
        Index("ix_sponsor_flows_proxy_key", "proxy_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The entry in @MTProxybot, once it gave a tag; NULL before (the proxy is a candidate).
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mtproxy_registrations.id", ondelete="CASCADE"),
        unique=True,
    )
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    user_name: Mapped[str] = mapped_column(String(64), nullable=False)  # the Telemt user
    # The live proxy using this row now: the provider's stable key
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
    # The tag is in the desired Telemt state of ``proxy_key`` (apply_ad_tag returned); a failed try
    # is repeated after a pause, since the provider does not list a registered proxy again.
    tag_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tag_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MtproxybotEntry(Base):
    """One proxy in @MTProxybot's own /myproxies list, as the operator account last read it
    (proxyhub/sponsor/inventory.py). The latest complete read replaces the rows; what the panel
    learnt about a button (its screen, a removal) follows it by ``(label_fp, ordinal)``.

    Nothing secret: the address the button names, the label as a shape (hex replaced) and a
    one-way fingerprint of it; never the label's hex, a proxy secret or a tag."""

    __tablename__ = "mtproxybot_entries"
    __table_args__ = (
        CheckConstraint("port IS NULL OR (port >= 1 AND port <= 65535)", name="port"),
        CheckConstraint(
            "removal_state IS NULL OR removal_state IN (" + ", ".join(f"'{s}'" for s in REMOVAL_STATES) + ")",
            name="removal_state",
        ),
        UniqueConstraint("label_fp", "ordinal", name="uq_mtproxybot_entries_label"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)  # NULL: the label names the host only
    label_shape: Mapped[str] = mapped_column(String(200), nullable=False)
    label_fp: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # nth button with this label
    # The registration whose tag the label's hex starts or ends (an exact match), if any.
    tag_registration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mtproxy_registrations.id", ondelete="SET NULL")
    )
    page: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    pressable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The proxy's own screen (live proxies: the Proxy Sponsor it names).
    menu_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    menu_channel: Mapped[str | None] = mapped_column(String(32))
    menu_note: Mapped[str | None] = mapped_column(String(60))
    # The cleanup: observed (its screen seen, nothing pressed), removed, unconfirmed, stuck, or
    # interrupted (a removal button was pressed and the walk did not finish).
    removal_state: Mapped[str | None] = mapped_column(String(16))
    removal_reason: Mapped[str | None] = mapped_column(String(60))
    removal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
