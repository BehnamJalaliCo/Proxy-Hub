"""Fleet team tables: secret vault, live Hetzner observations, live prices, dry-run plans.

Nothing here stores a user IP or user traffic (CLAUDE.md rule 7). The only servers recorded are
our own (label ``managed-by=proxy-hub``); servers we do not manage are only *counted*, never
named or stored (CLAUDE.md rule 4: the system should not even know they exist).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CIDR, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base


class VaultSecret(Base):
    """One secret, encrypted with AES-256-GCM under the panel master key (proxyhub/vault.py).

    The plaintext never touches the database; ``name`` is bound to the ciphertext as associated
    data, so a ciphertext copied to another row does not decrypt.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        CheckConstraint("octet_length(nonce) = 12", name="nonce_len"),
        CheckConstraint("octet_length(ciphertext) >= 16", name="ciphertext_len"),
        CheckConstraint("key_version >= 1", name="key_version"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountObservation(Base):
    """Last live look at one Hetzner account (account_guard reconciler and account verification)."""

    __tablename__ = "hetzner_account_observations"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hetzner_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # True when the last observation succeeded; then the counters below describe it.
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    guard_ok: Mapped[bool | None] = mapped_column(Boolean)
    servers_total: Mapped[int | None] = mapped_column(Integer)
    managed_servers: Mapped[int | None] = mapped_column(Integer)
    foreign_servers: Mapped[int | None] = mapped_column(Integer)
    # Short machine code of the last failure (never an exception text, never a secret).
    error: Mapped[str | None] = mapped_column(String(60))
    # Last time the observation succeeded (kept across failed attempts).
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountServer(Base):
    """One of OUR servers (label managed-by=proxy-hub) as Hetzner reported it at the last check."""

    __tablename__ = "hetzner_account_servers"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hetzner_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    hetzner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delete_protected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HetznerOffer(Base):
    """Live price of one server type in one location, as read with one account's token (pricing loop)."""

    __tablename__ = "hetzner_offers"
    __table_args__ = (
        CheckConstraint("price_hourly_net >= 0 AND price_monthly_net >= 0", name="price_non_negative"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hetzner_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    server_type: Mapped[str] = mapped_column(String(40), primary_key=True)
    location: Mapped[str] = mapped_column(String(20), primary_key=True)
    # Hetzner returns prices as strings with 10 decimal places; keep them exact.
    price_hourly_net: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    price_monthly_net: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    included_traffic_bytes: Mapped[int | None] = mapped_column(BigInteger)
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # None: Hetzner did not say (older API shape); False: listed but not orderable there now.
    available: Mapped[bool | None] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FleetPlan(Base):
    """One run of the fleet loop: what it would do (dry-run) and why."""

    __tablename__ = "fleet_plans"
    __table_args__ = (Index("ix_fleet_plans_generated_at", "generated_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # sha256 of the decision part of the plan (action, target, allowed, reasons).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # True when this run wrote its actions to actions_log (on change, or hourly when unchanged).
    logged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


# --- round 3: the live fleet (D14) and lost domain delegations --------------------------------

BURN_REASONS = ("probe_blocked",)
DELEGATION_RESULTS = ("ok", "nxdomain", "not_cloudflare", "ns_changed", "no_answer", "servfail", "error")
DELEGATION_VERDICTS = ("ok", "lost", "unknown")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class IpHistory(Base):
    """Every public IPv4 one of OUR servers got, and every one that burned (blocked from Iran),
    with its /24 (docs/10 section 3.11, decision D14). A new server that gets a burned IP, or an IP
    in a burned /24, is deleted at once and the creation is retried elsewhere.

    Only our own servers' addresses are here (Hetzner assigned them to us); nothing about any user
    (CLAUDE.md rule 7)."""

    __tablename__ = "ip_history"
    __table_args__ = (
        UniqueConstraint("node_id", "ip", name="uq_ip_history_node_ip"),
        CheckConstraint("family(ip) = 4 AND masklen(ip) = 32", name="ipv4_host"),
        CheckConstraint("prefix24 = network(set_masklen(ip, 24))", name="prefix24"),
        CheckConstraint("(burned_at IS NULL) = (burned_reason IS NULL)", name="burn_pair"),
        CheckConstraint(
            f"burned_reason IS NULL OR burned_reason IN ({_in(BURN_REASONS)})", name="burn_reason"
        ),
        Index("ix_ip_history_ip", "ip"),
        Index("ix_ip_history_prefix24_burned", "prefix24", postgresql_where="burned_at IS NOT NULL"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ip: Mapped[str] = mapped_column(INET, nullable=False)
    prefix24: Mapped[str] = mapped_column(CIDR, nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hetzner_accounts.id", ondelete="SET NULL")
    )
    location: Mapped[str] = mapped_column(String(20), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    burned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    burned_reason: Mapped[str | None] = mapped_column(String(30))


class FleetAlert(Base):
    """One kind of owner alert of the live fleet (a cap blocks a needed action, ...): when it was
    first and last seen and when the owner was last told, so an alert is repeated rarely."""

    __tablename__ = "fleet_alerts"
    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class DelegationCheck(Base):
    """One DNS-over-HTTPS NS lookup of a pool domain (proxyhub/cloudflare/delegation.py): is the
    domain still registered and delegated to Cloudflare? Only public name-server names are kept."""

    __tablename__ = "domain_delegation_checks"
    __table_args__ = (
        CheckConstraint(f"result IN ({_in(DELEGATION_RESULTS)})", name="result"),
        CheckConstraint(f"verdict IN ({_in(DELEGATION_VERDICTS)})", name="verdict"),
        Index("ix_domain_delegation_checks_zone_at", "zone_id", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    zone_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("cf_zones.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pool_state: Mapped[str] = mapped_column(String(10), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    rcode: Mapped[int | None] = mapped_column(SmallInteger)  # the DNS answer's Status (RCODE)
    nameservers: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)


class DelegationState(Base):
    """Per zone: the Cloudflare name servers first seen while the delegation was good (a later,
    different set means someone else's zone), and whether the panel took the zone out of the pool
    because its delegation was lost (it puts it back by itself when the delegation returns)."""

    __tablename__ = "domain_delegation_state"
    zone_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("cf_zones.id", ondelete="CASCADE"), primary_key=True
    )
    baseline_ns: Mapped[list[Any] | None] = mapped_column(JSONB)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_from: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
