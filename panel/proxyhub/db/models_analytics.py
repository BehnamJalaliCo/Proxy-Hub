"""Analytics tables: how much our servers serve and how loaded they are, how the channels grow.

Aggregates only (CLAUDE.md rule 7). A node sample holds Hetzner's own totals for one whole server
(bytes of the billing period, CPU percent, bytes per second); nothing is per connection, per proxy
user or per IP. A member event holds exactly four facts about a channel member, from Telegram's
``chat_member`` update (decision D7): the numeric Telegram user id, the time, join or leave, and the
NAME of the invite link used (or "folder"/"other"). No first name, username, phone, language,
premium flag or anything else about the person is stored anywhere.

History outlives the node: ``analytics_node_samples.node_id`` has no foreign key and each sample
carries the node's name, type and location, so a retired (or even removed) node row never takes its
traffic history with it.

Proxy usage (decision D39) comes from the node agents as counters only: per proxy/config and hour,
unique users per day, per country / region / ISP with every bucket under 3 users folded into
"other", and a per-user MB distribution (p50/p90/p99). Channel statistics (decision D38) are
Telegram's own aggregates read by the operator account. Neither holds an IP or a person.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

SOURCE_KINDS = ("link", "folder", "other")
EVENT_KINDS = ("join", "leave")
ALERT_KINDS = ("traffic", "cpu")


class NodeSample(Base):
    """One 15-minute look at one of OUR servers (label managed-by=proxy-hub, row in ``nodes``)."""

    __tablename__ = "analytics_node_samples"
    __table_args__ = (
        UniqueConstraint("node_id", "at", name="uq_analytics_node_samples_node_at"),
        Index("ix_analytics_node_samples_at", "at"),
        CheckConstraint(
            "(out_bytes IS NULL OR out_bytes >= 0) AND (in_bytes IS NULL OR in_bytes >= 0)"
            " AND (included_bytes IS NULL OR included_bytes >= 0)"
            " AND (out_delta_bytes IS NULL OR out_delta_bytes >= 0)"
            " AND (in_delta_bytes IS NULL OR in_delta_bytes >= 0)",
            name="bytes_non_negative",
        ),
        CheckConstraint(
            "(cpu_avg IS NULL OR cpu_avg >= 0) AND (cpu_max IS NULL OR cpu_max >= 0)"
            " AND (net_out_bps_avg IS NULL OR net_out_bps_avg >= 0)"
            " AND (net_in_bps_avg IS NULL OR net_in_bps_avg >= 0)",
            name="load_non_negative",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    hetzner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_name: Mapped[str] = mapped_column(String(100), nullable=False)
    server_type: Mapped[str] = mapped_column(String(40), nullable=False)
    location: Mapped[str] = mapped_column(String(20), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Hetzner's counters for the current billing period (bytes, whole server).
    out_bytes: Mapped[int | None] = mapped_column(BigInteger)
    in_bytes: Mapped[int | None] = mapped_column(BigInteger)
    included_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Bytes since the previous sample of the same node (a counter reset counts from zero).
    out_delta_bytes: Mapped[int | None] = mapped_column(BigInteger)
    in_delta_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Hetzner's price of one extra TB of traffic at this location (net EUR), from the server object.
    traffic_price_per_tb_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    # Percent of the WHOLE server (Hetzner's per-vCPU value divided by the vCPU count), over the window.
    cpu_avg: Mapped[float | None] = mapped_column(Float)
    cpu_max: Mapped[float | None] = mapped_column(Float)
    net_out_bps_avg: Mapped[float | None] = mapped_column(Float)
    net_in_bps_avg: Mapped[float | None] = mapped_column(Float)
    window_s: Mapped[int | None] = mapped_column(Integer)


class ChannelSample(Base):
    """Member count of one of our channels (getChatMemberCount), hourly."""

    __tablename__ = "analytics_channel_samples"
    __table_args__ = (
        CheckConstraint("role IN ('main', 'test')", name="role"),
        CheckConstraint("member_count >= 0", name="member_count"),
        UniqueConstraint("chat_id", "at", name="uq_analytics_channel_samples_chat_at"),
        Index("ix_analytics_channel_samples_role_at", "role", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)


class TrackingLink(Base):
    """A named invite link of the main channel the owner made for one campaign ("where they came from").

    Never deleted and never revoked: its joins stay countable forever. ``invite_link`` is our own
    link (``https://t.me/+...``), not anything about a person.
    """

    __tablename__ = "analytics_tracking_links"
    __table_args__ = (
        UniqueConstraint("chat_id", "name", name="uq_analytics_tracking_links_chat_name"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 32", name="name_len"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    invite_link: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Name the link has in Telegram (set when it was created; a rename here is a dashboard label).
    telegram_name: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    renamed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemberEvent(Base):
    """One join or leave in one of our channels. Exactly the D7 fields, nothing else about the person.

    A leave carries the source of the same person's latest earlier join in that channel, so
    "how many that came from link X have left" is a plain sum.
    """

    __tablename__ = "analytics_member_events"
    __table_args__ = (
        CheckConstraint("role IN ('main', 'test')", name="role"),
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in EVENT_KINDS)})", name="kind"),
        CheckConstraint(f"source IN ({', '.join(repr(s) for s in SOURCE_KINDS)})", name="source"),
        CheckConstraint("link_name IS NULL OR char_length(link_name) <= 32", name="link_name_len"),
        # A repeated update (or a re-read after a crash) is stored once.
        UniqueConstraint(
            "chat_id", "telegram_user_id", "kind", "at", name="uq_analytics_member_events_natural"
        ),
        Index("ix_analytics_member_events_chat_at", "chat_id", "at"),
        Index("ix_analytics_member_events_user", "chat_id", "telegram_user_id", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(5), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    link_name: Mapped[str | None] = mapped_column(String(32))
    tracking_link_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("analytics_tracking_links.id", ondelete="RESTRICT")
    )


class ThresholdAlert(Base):
    """One owner alert about one node and one limit on one Tehran day (so at most one a day)."""

    __tablename__ = "analytics_alerts"
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in ALERT_KINDS)})", name="kind"),
        UniqueConstraint("node_id", "kind", "day", name="uq_analytics_alerts_node_kind_day"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    outbox_id: Mapped[int | None] = mapped_column(BigInteger)


# --- proxy usage from the node agents (``usage_agg`` in the agent report; decision D39) -----------
#
# The agent turns each connection's IP into counters IN MEMORY on the node and sends only these
# counters. Unique users are counted with an HMAC of the IP under a per-day random salt that never
# leaves the node's memory. Every bucket with fewer than USAGE_MIN_USERS users is folded into
# "other" (by the agent, and again here), so no row describes one person.

USAGE_KINDS = ("mtproto", "reality")
USAGE_DIMS = ("country", "region", "asn")
USAGE_MIN_USERS = 3


class UsageHour(Base):
    """Bytes and peak connections of one proxy (Telemt user) or Reality config in one UTC hour."""

    __tablename__ = "analytics_usage_hourly"
    __table_args__ = (
        CheckConstraint("hour BETWEEN 0 AND 23", name="hour"),
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in USAGE_KINDS)})", name="kind"),
        CheckConstraint("bytes_up >= 0 AND bytes_down >= 0 AND conns_max >= 0", name="counters"),
        UniqueConstraint(
            "node_id", "boot_id", "day", "hour", "kind", "ref", name="uq_analytics_usage_hourly_key"
        ),
        Index("ix_analytics_usage_hourly_day", "day"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Random per agent process start: counters of two runs on one day are added, never overwritten.
    boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # Telemt user name or Reality config name: a shared proxy, never a person.
    ref: Mapped[str] = mapped_column(String(32), nullable=False)
    bytes_up: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes_down: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conns_max: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageDay(Base):
    """Unique users of one node on one UTC day, and how much a user typically uses (MB)."""

    __tablename__ = "analytics_usage_daily"
    __table_args__ = (
        CheckConstraint("unique_users >= 0", name="users"),
        CheckConstraint(
            "(p50_mb IS NULL OR p50_mb >= 0) AND (p90_mb IS NULL OR p90_mb >= 0)"
            " AND (p99_mb IS NULL OR p99_mb >= 0)",
            name="percentiles",
        ),
        UniqueConstraint("node_id", "boot_id", "day", name="uq_analytics_usage_daily_key"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    unique_users: Mapped[int] = mapped_column(Integer, nullable=False)
    # Distribution of MB per anonymous user that day; NULL below USAGE_MIN_USERS users.
    p50_mb: Mapped[float | None] = mapped_column(Float)
    p90_mb: Mapped[float | None] = mapped_column(Float)
    p99_mb: Mapped[float | None] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageDim(Base):
    """Unique users and bytes of one node and UTC day per country, province/region or ISP (ASN)."""

    __tablename__ = "analytics_usage_dims"
    __table_args__ = (
        CheckConstraint(f"dim IN ({', '.join(repr(d) for d in USAGE_DIMS)})", name="dim"),
        CheckConstraint(f"key = 'other' OR users >= {USAGE_MIN_USERS}", name="k_anonymity"),
        CheckConstraint("users >= 0 AND bytes >= 0", name="counters"),
        UniqueConstraint("node_id", "boot_id", "day", "dim", "key", name="uq_analytics_usage_dims_key"),
        Index("ix_analytics_usage_dims_day", "day", "dim"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    dim: Mapped[str] = mapped_column(String(10), nullable=False)
    # "IR" (country), "Tehran" (region, as the offline DB names it), "AS197207" (ASN), or "other".
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    users: Mapped[int] = mapped_column(Integer, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- channel statistics through the operator account (decision D38) -------------------------------


class ChannelStatsSnapshot(Base):
    """One read of stats.getBroadcastStats (+ channels.getFullChannel) for one of our channels.

    ``graphs`` holds Telegram's own aggregate graphs, parsed to ``{name: {"x": [...], "series":
    {label: [...]}}}`` (followers, views by source, new followers by source, languages, top hours).
    """

    __tablename__ = "analytics_channel_stats"
    __table_args__ = (
        CheckConstraint("role IN ('main', 'test')", name="role"),
        Index("ix_analytics_channel_stats_role_at", "role", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # ok | stats_dc_other | stats_unavailable | <error code of the failed call>
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    participants: Mapped[int | None] = mapped_column(Integer)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followers: Mapped[float | None] = mapped_column(Float)
    followers_prev: Mapped[float | None] = mapped_column(Float)
    views_per_post: Mapped[float | None] = mapped_column(Float)
    views_per_post_prev: Mapped[float | None] = mapped_column(Float)
    shares_per_post: Mapped[float | None] = mapped_column(Float)
    shares_per_post_prev: Mapped[float | None] = mapped_column(Float)
    reactions_per_post: Mapped[float | None] = mapped_column(Float)
    reactions_per_post_prev: Mapped[float | None] = mapped_column(Float)
    notifications_pct: Mapped[float | None] = mapped_column(Float)
    graphs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class PostViews(Base):
    """Latest views/forwards of one of our channel posts (messages.getMessagesViews or the stats)."""

    __tablename__ = "analytics_post_views"
    __table_args__ = (
        CheckConstraint("role IN ('main', 'test')", name="role"),
        CheckConstraint("views >= 0 AND forwards >= 0", name="counters"),
        UniqueConstraint("chat_id", "message_id", name="uq_analytics_post_views_chat_message"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    views: Mapped[int] = mapped_column(Integer, nullable=False)
    forwards: Mapped[int] = mapped_column(Integer, nullable=False)
    reactions: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MtproxybotStat(Base):
    """STUB (round 4): per-proxy numbers @MTProxybot shows the operator account. Nothing writes it yet."""

    __tablename__ = "analytics_mtproxybot_stats"
    __table_args__ = (Index("ix_analytics_mtproxybot_stats_at", "at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metric: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
