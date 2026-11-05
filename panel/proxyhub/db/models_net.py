"""Net team tables: Cloudflare DNS with the domain pool (docs/10 section 3.10, decision D13) and
reachability probes from Iran (docs/10 M4.1).

Rule 3: the Cloudflare token lives only in the vault (``secrets``, name ``cloudflare:token``);
nothing here holds it. Rule 7: no end-user data exists in any of these tables. Probe results are
per measurement network (a public probe's ASN or a check-host node name), never a person; the
only IP addresses stored are our own nodes' and the answers DNS gave for our own names.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

POOL_STATES = ("none", "active", "standby", "filtered", "retired", "deleted")
RECORD_TYPES = ("A", "AAAA", "TXT")
# slot: node names (slot<N>.<domain> and a test node's t<8 hex>.<domain>, the DNS loop); acme: the
# _acme-challenge TXT records of a certificate order (proxyhub/acme); test: real Cloudflare tests.
RECORD_PURPOSES = ("slot", "test", "acme")
PROBE_SOURCES = ("globalping", "check-host")
PROBE_KINDS = ("tcp", "icmp", "dns")
PROBE_ROLES = ("node", "control")
MEASUREMENT_STATUSES = ("ok", "no_probes", "rate_limited", "error", "timeout", "skipped")
VERDICTS = ("ok", "suspect", "blocked", "unknown")
ROUND_TRIGGERS = ("schedule", "manual")
DOMAIN_VERDICTS = ("filtered", "clear", "unknown")
DOMAIN_CHECK_STATUSES = ("ok", "no_probes", "rate_limited", "error", "timeout")
DOMAIN_CHECK_COUNTS = (
    "probes_asked >= 0 AND probes >= 0 AND answered >= 0 AND hijacked >= 0 AND hijacked <= answered"
    " AND networks_answered >= 0 AND networks_hijacked >= 0 AND networks_hijacked <= networks_answered"
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --- Cloudflare ---------------------------------------------------------------------------------


class CfState(Base):
    """The single row describing the stored Cloudflare token's last verification (no token here)."""

    __tablename__ = "cf_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("token_kind IS NULL OR token_kind IN ('user', 'account')", name="token_kind"),
    )
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, default=1)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last problem as a contract error code (cf_token_invalid, cf_token_multiple_accounts,
    # cf_permissions_missing, cf_unavailable).
    error: Mapped[str | None] = mapped_column(String(40))
    missing: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    token_kind: Mapped[str | None] = mapped_column(String(10))
    zones_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the pool ran out of usable standby domains and the owner was told; the reminder is
    # repeated at most once a day while it stays empty, and cleared when a standby exists again.
    standby_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


class CfZone(Base):
    """A Cloudflare zone the token can see, with its place in the domain pool (decision D35).

    ``created_at`` is when the panel first saw the zone (promotion order). A zone that is still
    ``none`` and was never moved (``pool_changed_at`` NULL) joins the pool by itself once
    Cloudflare serves it; the filter checks move it to ``filtered``; a filtered zone is deleted from
    Cloudflare after a grace period and ends as ``deleted`` (proxyhub/cloudflare/deletion.py).
    """

    __tablename__ = "cf_zones"
    __table_args__ = (
        CheckConstraint(f"pool_state IN ({_in(POOL_STATES)})", name="pool_state"),
        CheckConstraint("id ~ '^[0-9a-f]{32}$'", name="id_hex"),
        # At most one active domain, enforced by the database itself.
        Index(
            "uq_cf_zones_one_active",
            "pool_state",
            unique=True,
            postgresql_where=text("pool_state = 'active'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(253), nullable=False)
    # Cloudflare's own zone status (active, pending, initializing, moved, ...) or 'gone' when the
    # token no longer lists it.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    pool_state: Mapped[str] = mapped_column(String(10), nullable=False, default="none")
    pool_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # DNS:Edit confirmed for this zone without changing anything (see cloudflare/client.py).
    dns_edit: Mapped[bool | None] = mapped_column(Boolean)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the zone entered 'filtered' (the deletion grace period starts here).
    filtered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the panel deleted the zone from Cloudflare (pool_state 'deleted').
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A deletion attempt that is in flight (committed before the API call; expires after a while).
    delete_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cloudflare refused the deletion for good (zone_edit_denied, cf_refused:<codes>): not retried
    # until a token is stored again.
    delete_blocked: Mapped[str | None] = mapped_column(String(60))
    # Transient failures: error kind, count, and when to try again (backoff).
    delete_error: Mapped[str | None] = mapped_column(String(30))
    delete_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    delete_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class CfRecord(Base):
    """A DNS record this panel created. The panel only ever changes or deletes a record that has
    a row here AND carries the comment marker ``managed-by=proxy-hub`` in Cloudflare.

    ``id`` is also written into the record comment (``ref=<id>``), so a record created just before
    a crash (row written, Cloudflare id not yet stored) is found and adopted instead of duplicated.
    """

    __tablename__ = "cf_records"
    __table_args__ = (
        UniqueConstraint("zone_id", "name", "type", name="uq_cf_records_zone_name_type"),
        CheckConstraint(f"type IN ({_in(RECORD_TYPES)})", name="type"),
        CheckConstraint(f"purpose IN ({_in(RECORD_PURPOSES)})", name="purpose"),
        CheckConstraint("ttl = 1 OR ttl BETWEEN 60 AND 86400", name="ttl"),
        CheckConstraint("cf_record_id IS NULL OR cf_record_id ~ '^[0-9a-f]{32}$'", name="cf_id_hex"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zone_id: Mapped[str] = mapped_column(ForeignKey("cf_zones.id"), nullable=False)
    cf_record_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(253), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ttl: Mapped[int] = mapped_column(Integer, nullable=False)
    proxied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    purpose: Mapped[str] = mapped_column(String(10), nullable=False, default="slot")
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class DomainCheck(Base):
    """One filter check of a domain from Iran: a Globalping DNS (A) query of the zone's apex name
    through Iranian probes' own resolvers. An answer in 10.10.34.0/24 is the Iranian filter's
    hijack signature. Only counts per measurement network are kept (rule 7): no answer addresses,
    no probe addresses, nothing about any user."""

    __tablename__ = "domain_checks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(DOMAIN_CHECK_STATUSES)})", name="status"),
        CheckConstraint(f"verdict IN ({_in(DOMAIN_VERDICTS)})", name="verdict"),
        CheckConstraint(f"pool_state IN ({_in(POOL_STATES)})", name="pool_state"),
        CheckConstraint(DOMAIN_CHECK_COUNTS, name="counts"),
        Index("ix_domain_checks_zone_at", "zone_id", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    zone_id: Mapped[str] = mapped_column(ForeignKey("cf_zones.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # measurement start
    pool_state: Mapped[str] = mapped_column(String(10), nullable=False)  # the zone's state when checked
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(64))  # Globalping measurement id
    probes_asked: Mapped[int] = mapped_column(Integer, nullable=False)
    probes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    answered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hijacked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    networks_answered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    networks_hijacked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{"asn": 197207, "network": "...", "answered": 1, "hijacked": 1}, ...]
    detail: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    # The domain's verdict computed right after this round (proxyhub/cloudflare/filtering.py).
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)
    verdict_reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)


# --- Probes from Iran ---------------------------------------------------------------------------


class ProbeCycle(Base):
    """One reconciler pass: the shared control measurements plus the rounds of the nodes due."""

    __tablename__ = "probe_cycles"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProbeMeasurement(Base):
    """One measurement at one source (a Globalping measurement or a check-host check)."""

    __tablename__ = "probe_measurements"
    __table_args__ = (
        CheckConstraint(f"source IN ({_in(PROBE_SOURCES)})", name="source"),
        CheckConstraint(f"kind IN ({_in(PROBE_KINDS)})", name="kind"),
        CheckConstraint(f"role IN ({_in(PROBE_ROLES)})", name="role"),
        CheckConstraint(f"status IN ({_in(MEASUREMENT_STATUSES)})", name="status"),
        CheckConstraint("(role = 'node') = (node_id IS NOT NULL)", name="role_node"),
        Index("ix_probe_measurements_cycle", "cycle_id"),
        Index("ix_probe_measurements_node_at", "node_id", "at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("probe_cycles.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    # Our node's IP, the control host, or one of our own DNS names. Never an end-user address.
    target: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(64))  # Globalping id / check-host request id
    note: Mapped[str | None] = mapped_column(String(200))


class ProbeNetworkResult(Base):
    """What one measurement saw from one network: connection attempts that worked or failed."""

    __tablename__ = "probe_network_results"
    __table_args__ = (
        CheckConstraint("ok >= 0 AND fail >= 0 AND nodata >= 0", name="counts"),
        Index("ix_probe_network_results_measurement", "measurement_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    measurement_id: Mapped[int] = mapped_column(
        ForeignKey("probe_measurements.id", ondelete="CASCADE"), nullable=False
    )
    network_key: Mapped[str] = mapped_column(String(64), nullable=False)  # 'asn:197207' or 'ch:ir1'
    network: Mapped[str] = mapped_column(String(100), nullable=False)
    asn: Mapped[int | None] = mapped_column(BigInteger)
    ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodata: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rtt_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class ProbeRound(Base):
    """One probe round of one node: both sources' observations and the verdict computed after it."""

    __tablename__ = "probe_rounds"
    __table_args__ = (
        UniqueConstraint("cycle_id", "node_id", name="uq_probe_rounds_cycle_node"),
        CheckConstraint(f"verdict IN ({_in(VERDICTS)})", name="verdict"),
        CheckConstraint(f"trigger IN ({_in(ROUND_TRIGGERS)})", name="trigger"),
        Index("ix_probe_rounds_node_started", "node_id", "started_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("probe_cycles.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)
    target_ip: Mapped[str] = mapped_column(INET, nullable=False)  # our node's IPv4 at that time
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    # {"globalping": {"state": ..., "reason": ...}, "check-host": {...}} (see probes/verdict.py)
    observations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)
    verdict_reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)


class ProbeNodeState(Base):
    """Latest verdict per node, and a pending owner request for an immediate round."""

    __tablename__ = "probe_node_state"
    __table_args__ = (CheckConstraint(f"verdict IN ({_in(VERDICTS)})", name="verdict"),)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)
    last_round_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verdict: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verdict_reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)


class ProbeSource(Base):
    """Health of each probe source as last seen, and Globalping's rate-limit headers."""

    __tablename__ = "probe_sources"
    __table_args__ = (CheckConstraint(f"name IN ({_in(PROBE_SOURCES)})", name="name"),)
    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(200))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ratelimit_limit: Mapped[int | None] = mapped_column(Integer)
    ratelimit_remaining: Mapped[int | None] = mapped_column(Integer)
    ratelimit_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
