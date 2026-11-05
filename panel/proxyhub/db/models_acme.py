"""ACME certificates (Let's Encrypt, DNS-01) for the panel's own pool domains (proxyhub/acme).

One row per domain: the wildcard certificate ``*.<domain>`` the nodes serve for their own DNS names
(VLESS + WebSocket + TLS, and the TLS front of Telemt). Only public facts and bookkeeping are here;
the certificate chain and its private key live in the vault (``acme:cert:<domain>:chain`` and
``acme:cert:<domain>:key``, rule 3), and so does the ACME account key (``acme:account-key``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base


class AcmeCertificate(Base):
    __tablename__ = "acme_certificates"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint("(not_before IS NULL) = (not_after IS NULL)", name="validity_pair"),
        CheckConstraint("fingerprint_sha256 IS NULL OR fingerprint_sha256 ~ '^[0-9a-f]{64}$'", name="fp_hex"),
    )
    # The pool domain (a Cloudflare zone name); the certificate covers *.<domain>.
    domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    zone_id: Mapped[str | None] = mapped_column(String(32))
    # The certificate in the vault now (NULL until the first issuance).
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    serial_hex: Mapped[str | None] = mapped_column(String(64))
    fingerprint_sha256: Mapped[str | None] = mapped_column(String(64))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Orders: one at a time per domain, with backoff after a failure (Let's Encrypt rate limits).
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A short error code of the last failed order (never a message from the CA or a key).
    last_error: Mapped[str | None] = mapped_column(String(60))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
