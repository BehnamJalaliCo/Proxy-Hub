"""Reality team tables: VLESS + REALITY on every ready node (docs/10 sections 3.5 and 3.14, D18).

* ``reality_keys``: one REALITY key pair per key scope. A scope is a slot (slot<N> keeps its key
  when a new server takes the slot over, so a posted ``slot<N>.<domain>`` link keeps working, as
  docs/10 M2.6 requires) or, for a node without a slot (the owner's test node), the node itself.
  The private key is only in the vault (``secrets``, name ``reality:key:slot:<N>`` or
  ``reality:key:node:<uuid>``, AES-256-GCM, rule 3); the public key, short id and target are public
  link data. A scope follows the owner's target setting once none of its configs needs the old one
  (proxyhub/reality/policy.py ``retarget_scopes``).
* ``reality_configs``: short-lived configs (a UUID each), 48 h or 1 TB by default (D41), with the
  link host and the transport fixed when the config is made (``reality``: slot name, else the
  node's IPv4; ``ws_tls``: the node's DNS name, ``slot<N>.<domain>`` or ``t<8 hex>.<domain>``).
* ``reality_usage``: per config and node, the agent's cumulative byte count (aggregate per
  config, never a person) and the last value reported, so a reset agent is added up correctly.
* ``reality_node_status``: what each node's agent says about Xray (up, version, and its target
  check together with the target and server name that check was about).

Rule 7: there is no end-user data anywhere here; the only addresses are our own nodes' (in a
config's link host).
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
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

CONFIG_STATUSES = ("active", "expired", "full", "burned")
# How a config's link reaches the node: VLESS + REALITY on TCP, or VLESS + WebSocket + TLS with the
# panel's own certificate for the node's DNS name (proxyhub/reality/settings.py ``transport``).
TRANSPORTS = ("reality", "ws_tls")
PUBLIC_KEY_RE = "^[A-Za-z0-9_-]{43}$"
SHORT_ID_RE = "^([0-9a-f]{2}){1,8}$"
WS_PATH_RE = "^/[0-9a-f]{16}$"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RealityKey(Base):
    __tablename__ = "reality_keys"
    __table_args__ = (
        CheckConstraint("(slot IS NULL) <> (node_id IS NULL)", name="one_scope"),
        CheckConstraint("slot IS NULL OR slot >= 1", name="slot_positive"),
        CheckConstraint(f"public_key ~ '{PUBLIC_KEY_RE}'", name="public_key"),
        CheckConstraint(f"short_id ~ '{SHORT_ID_RE}'", name="short_id"),
        CheckConstraint(f"ws_path ~ '{WS_PATH_RE}'", name="ws_path"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slot: Mapped[int | None] = mapped_column(Integer, unique=True)
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), unique=True
    )
    public_key: Mapped[str] = mapped_column(String(43), nullable=False)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False)
    # host:port of the REALITY target and the server name (SNI) clients send.
    target: Mapped[str] = mapped_column(String(260), nullable=False)
    server_name: Mapped[str] = mapped_column(String(253), nullable=False)
    # Vault name of the private key (never the key itself).
    secret_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # The scope's WebSocket path for the ws_tls transport: "/" + 16 random hex, made with the key, so
    # a slot's links keep their path when a new server takes the slot over.
    ws_path: Mapped[str] = mapped_column(String(17), nullable=False)
    created_at: Mapped[datetime] = _now()


class RealityConfig(Base):
    __tablename__ = "reality_configs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(CONFIG_STATUSES)})", name="status"),
        CheckConstraint("(status = 'active') = (ended_at IS NULL)", name="ended_pair"),
        CheckConstraint("data_cap_bytes > 0 AND used_bytes >= 0", name="bytes"),
        CheckConstraint("expires_at > created_at", name="window"),
        CheckConstraint("slot IS NULL OR slot >= 1", name="slot_positive"),
        CheckConstraint(f"transport IN ({_in(TRANSPORTS)})", name="transport"),
        Index("ix_reality_configs_key_status", "key_id", "status"),
        Index("ix_reality_configs_status_ended", "status", "ended_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # The node the config was made for (its IPv4 is the host when there is no slot name).
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int] = mapped_column(ForeignKey("reality_keys.id", ondelete="CASCADE"), nullable=False)
    # The VLESS user id: whoever has the link may connect (it is published on purpose).
    uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_cap_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The inbound the config was made for; its link only works while its nodes serve that inbound.
    transport: Mapped[str] = mapped_column(
        String(10), nullable=False, default="reality", server_default="reality"
    )


class RealityUsage(Base):
    __tablename__ = "reality_usage"
    __table_args__ = (CheckConstraint("total_bytes >= 0 AND last_reported >= 0", name="bytes"),)
    config_id: Mapped[int] = mapped_column(
        ForeignKey("reality_configs.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_reported: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = _now()


class RealityNodeStatus(Base):
    __tablename__ = "reality_node_status"
    __table_args__ = (
        CheckConstraint("users_reported >= 0", name="users"),
        CheckConstraint(
            "(target_ok IS NULL) = (checked_target IS NULL) "
            "AND (checked_target IS NULL) = (checked_server_name IS NULL)",
            name="checked",
        ),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    up: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[str | None] = mapped_column(String(40))
    uptime_s: Mapped[float | None] = mapped_column(Float)
    # The node's last REALITY target check: its result and the target and server name it was about
    # (a result only counts for the scope's current target; all three are NULL until the first check).
    target_ok: Mapped[bool | None] = mapped_column(Boolean)
    checked_target: Mapped[str | None] = mapped_column(String(260))
    checked_server_name: Mapped[str | None] = mapped_column(String(253))
    apply_error: Mapped[str | None] = mapped_column(String(60))
    users_reported: Mapped[int] = mapped_column(Integer, nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
