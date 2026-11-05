"""Database schema (docs/07_DATA_MODEL.md, docs/10_IMPLEMENTATION_PLAN.md M1.2).

All times are UTC ``timestamptz``. Nothing here stores a user IP or user traffic
(CLAUDE.md rule 7); IPs in this schema belong to our own servers only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from proxyhub.fleet.lifecycle import NodeState

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ACCOUNT_STATES = ("verifying", "preparing", "active", "draining", "retired", "locked")


def _in(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB}  # noqa: RUF012 - SQLAlchemy class attribute


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Setting(Base):
    """Owner-controlled settings: goals, caps, texts, kill switch (typed in settings_schema.py)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _now_col()


class HetznerAccount(Base):
    """A separate Hetzner account/project for nodes (decision D9). Rotated every 1-2 months."""

    __tablename__ = "hetzner_accounts"
    __table_args__ = (CheckConstraint(f"state IN ({_in(ACCOUNT_STATES)})", name="state"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Name of the secret file (not its content) under the secrets directory.
    token_secret_name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="verifying")
    state_reason: Mapped[str | None] = mapped_column(Text)
    learned_server_limit: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in([s.value for s in NodeState])})", name="state"),
        UniqueConstraint("account_id", "hetzner_id", name="uq_nodes_account_hetzner"),
        Index("ix_nodes_state", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hetzner_accounts.id"), nullable=False)
    hetzner_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slot: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(String(20), nullable=False)
    server_type: Mapped[str] = mapped_column(String(20), nullable=False)
    ipv4: Mapped[str | None] = mapped_column(INET)
    ipv6: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=NodeState.REQUESTED.value)
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    burned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NodeEvent(Base):
    __tablename__ = "node_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), nullable=False, index=True)
    at: Mapped[datetime] = _now_col()
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class ActionLog(Base):
    """Every decision the system makes about real resources, with its reasons (dashboard 'events')."""

    __tablename__ = "actions_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _now_col()
    actor: Mapped[str] = mapped_column(String(60), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    target: Mapped[str | None] = mapped_column(String(200))
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[str | None] = mapped_column(Text)


class CostLedger(Base):
    __tablename__ = "cost_ledger"
    __table_args__ = (UniqueConstraint("day", "node_id", "source", name="uq_cost_ledger_day_node_source"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hetzner_accounts.id"), nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"))
    euros: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)


class TgUpdate(Base):
    """Raw Telegram update, stored once by update_id (idempotent webhook)."""

    __tablename__ = "tg_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    received_at: Mapped[datetime] = _now_col()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    """Durable queue of Telegram jobs (post, edit, invite link, alert; the ``operator_*`` posts and
    edits go through the operator account, decision D38). Never a delete job (rule 6)."""

    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('send_post', 'edit_post', 'create_invite_link', 'alert', "
            "'operator_send_post', 'operator_edit_post')",
            name="kind",
        ),
        Index("ix_outbox_pending", "not_before", postgresql_where="done_at IS NULL"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    not_before: Mapped[datetime] = _now_col()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col()
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- MTProto sponsorship (docs/11_MTPROTO_SPONSOR.md, decision D25: top priority) -------------

REGISTRATION_STATES = ("pending", "registered", "failed", "retired")
CAMPAIGN_STATES = ("draft", "scheduled", "active", "finished", "cancelled")


class MtproxyRegistration(Base):
    """One proxy registered in @MTProxybot: host:port + secret -> ad tag, with its current sponsor."""

    __tablename__ = "mtproxy_registrations"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in(REGISTRATION_STATES)})", name="state"),
        UniqueConstraint("host", "port", "secret_name", name="uq_mtproxy_reg_host_port_secret"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host: Mapped[str] = mapped_column(String(253), nullable=False)  # domain name or server IP (F2)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    secret_name: Mapped[str] = mapped_column(String(64), nullable=False)  # telemt access.users key
    ad_tag: Mapped[str | None] = mapped_column(String(32))  # 32 hex chars from @MTProxybot
    sponsor_channel: Mapped[str | None] = mapped_column(String(64))  # public @username
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    state_reason: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()


class SponsorCampaign(Base):
    """A paid sponsorship: a customer's public channel shown as Proxy Sponsor for a time window."""

    __tablename__ = "sponsor_campaigns"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in(CAMPAIGN_STATES)})", name="state"),
        CheckConstraint("ends_at > starts_at", name="window"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)  # public @username only
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    registration_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()


class MtproxyStat(Base):
    """Aggregate counters only (rule 7): never an IP, never a per-person record."""

    __tablename__ = "mtproxy_stats"
    __table_args__ = (Index("ix_mtproxy_stats_reg_at", "registration_id", "at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    registration_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mtproxy_registrations.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'node' | 'mtproxybot'
    current_connections: Mapped[int | None] = mapped_column(BigInteger)
    total_octets: Mapped[int | None] = mapped_column(BigInteger)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
