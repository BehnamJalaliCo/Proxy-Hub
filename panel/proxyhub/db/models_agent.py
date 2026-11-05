"""Agent team tables: node agent PKI, one-time registration, Telemt desired state and reports
(docs/10 section 3.5, docs/11_MTPROTO_SPONSOR.md; decision D25).

Rule 7: nothing here stores a user IP or a per-person record. Reports hold per-secret counters
(connections, octets) and node process/system counters only. Rule 3: registration tokens are
stored only as SHA-256, Telemt user secrets only AES-256-GCM sealed under the master key.
Certificates are public data (the private keys stay on the nodes).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

HEX32 = "^[0-9a-f]{32}$"
HEX64 = "^[0-9a-f]{64}$"
# Telemt user (secret) names: safe as TOML bare keys and Prometheus label values; never "include"
# (Telemt expands any line starting with ``include =`` before parsing TOML).
TELEMT_USER_NAME = "^[a-z][a-z0-9_]{0,31}$"
REVOKE_REASONS = ("re_registered", "superseded", "node_deleted", "manual")
ISSUED_VIA = ("register", "renew")


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentCaCertificate(Base):
    """The CA certificate built deterministically from the CA key, one row per CA key."""

    __tablename__ = "agent_ca_certificates"
    __table_args__ = (CheckConstraint(f"spki_sha256 ~ '{HEX64}'", name="spki_hex"),)
    spki_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()


class AgentRegistrationToken(Base):
    """One-time registration token for a node (1 hour). Only its SHA-256 is stored."""

    __tablename__ = "agent_registration_tokens"
    __table_args__ = (
        CheckConstraint(f"token_sha256 ~ '{HEX64}'", name="token_hex"),
        CheckConstraint("expires_at >= created_at", name="window"),
        Index("ix_agent_registration_tokens_node", "node_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentCertificate(Base):
    """A client certificate issued to a node agent. Authentication trusts only rows here that are
    unrevoked and inside their validity window."""

    __tablename__ = "agent_certificates"
    __table_args__ = (
        CheckConstraint(f"fingerprint_sha256 ~ '{HEX64}'", name="fingerprint_hex"),
        CheckConstraint("not_after > not_before", name="window"),
        CheckConstraint(f"issued_via IN ({', '.join(repr(v) for v in ISSUED_VIA)})", name="issued_via"),
        CheckConstraint(
            f"revoked_reason IS NULL OR revoked_reason IN ({', '.join(repr(v) for v in REVOKE_REASONS)})",
            name="revoked_reason",
        ),
        CheckConstraint("(revoked_at IS NULL) = (revoked_reason IS NULL)", name="revoked_pair"),
        Index("ix_agent_certificates_node", "node_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    serial_hex: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    ca_spki_sha256: Mapped[str] = mapped_column(
        ForeignKey("agent_ca_certificates.spki_sha256"), nullable=False
    )
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_via: Mapped[str] = mapped_column(String(10), nullable=False)
    issued_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(20))


class AgentNode(Base):
    """What the panel knows about a node's agent: versions, heartbeat, last applied state."""

    __tablename__ = "agent_nodes"
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)
    agent_version: Mapped[str] = mapped_column(String(40), nullable=False)
    telemt_version: Mapped[str | None] = mapped_column(String(40))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_served_etag: Mapped[str | None] = mapped_column(String(66))
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_applied_etag: Mapped[str | None] = mapped_column(String(66))
    last_apply_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_apply_error: Mapped[str | None] = mapped_column(String(60))


class TelemtNodeConfig(Base):
    """Node-wide Telemt settings of the desired state. The privacy and sponsor invariants
    (use_middle_proxy on, beobachten off, log_level silent) are constants in code, not columns."""

    __tablename__ = "telemt_node_configs"
    __table_args__ = (
        CheckConstraint("cardinality(ports) BETWEEN 1 AND 4", name="ports_count"),
        CheckConstraint("1 <= ALL(ports) AND 65535 >= ALL(ports)", name="ports_range"),
        CheckConstraint(f"ad_tag IS NULL OR ad_tag ~ '{HEX32}'", name="ad_tag_hex"),
        CheckConstraint("mode_classic OR mode_secure OR mode_tls", name="one_mode"),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True)
    ports: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    tls_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    ad_tag: Mapped[str | None] = mapped_column(String(32))
    mode_classic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode_secure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = _now()


class TelemtUser(Base):
    """One Telemt user (one MTProto secret) on one node, with its sponsor tag and limits."""

    __tablename__ = "telemt_users"
    __table_args__ = (
        UniqueConstraint("node_id", "name", name="uq_telemt_users_node_name"),
        CheckConstraint(f"name ~ '{TELEMT_USER_NAME}'", name="name"),
        CheckConstraint(f"ad_tag IS NULL OR ad_tag ~ '{HEX32}'", name="ad_tag_hex"),
        CheckConstraint("data_quota_bytes IS NULL OR data_quota_bytes > 0", name="quota_positive"),
        CheckConstraint("max_tcp_conns IS NULL OR max_tcp_conns > 0", name="conns_positive"),
        CheckConstraint("octet_length(secret_sealed) = 45", name="sealed_len"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    # AES-256-GCM: version byte + 12-byte nonce + the 16-byte secret + 16-byte tag (45 bytes).
    secret_sealed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ad_tag: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    max_tcp_conns: Mapped[int | None] = mapped_column(Integer)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mtproxy_registrations.id"))
    # Latest counters from the node (aggregate per secret; never a person, never an IP).
    last_current_connections: Mapped[int | None] = mapped_column(BigInteger)
    last_total_octets: Mapped[int | None] = mapped_column(BigInteger)
    last_stats_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class AgentNodeReport(Base):
    """Process and system counters from one agent report (no per-user data, no IPs)."""

    __tablename__ = "agent_node_reports"
    __table_args__ = (Index("ix_agent_node_reports_node_at", "node_id", "at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    at: Mapped[datetime] = _now()
    applied_etag: Mapped[str | None] = mapped_column(String(66))
    apply_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    apply_error: Mapped[str | None] = mapped_column(String(60))
    telemt_up: Mapped[bool] = mapped_column(Boolean, nullable=False)
    telemt_uptime_s: Mapped[float | None] = mapped_column(Float)
    connections_total: Mapped[int | None] = mapped_column(BigInteger)
    connections_bad_total: Mapped[int | None] = mapped_column(BigInteger)
    handshake_timeouts_total: Mapped[int | None] = mapped_column(BigInteger)
    # Telemt's throughput diagnostics (agent 0.4.0 on): middle-proxy data dropped for a full client
    # queue, and connections refused at max_connections (counters since Telemt started).
    me_route_drops_total: Mapped[int | None] = mapped_column(BigInteger)
    accept_permit_timeouts_total: Mapped[int | None] = mapped_column(BigInteger)
    users_reported: Mapped[int] = mapped_column(Integer, nullable=False)
    sys_uptime_s: Mapped[float | None] = mapped_column(Float)
    load1: Mapped[float | None] = mapped_column(Float)
    mem_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mem_available_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cpu_count: Mapped[int | None] = mapped_column(Integer)
