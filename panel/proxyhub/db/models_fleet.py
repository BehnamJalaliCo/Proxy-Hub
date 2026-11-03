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
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
