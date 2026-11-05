"""Certificates on record: public facts in ``acme_certificates``, the chain and key in the vault.

Readers (the desired state for the nodes, the Reality policy, the dashboard) only ever get a
certificate that is valid now; the key is opened only to put it into the desired state of a node's
own agent (over mTLS), never logged and never returned by an API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_acme import AcmeCertificate
from proxyhub.telegram.store import get_setting, put_setting
from proxyhub.vault import SecretValue, Vault, VaultError, delete_secret

Db = AsyncConnection | AsyncSession
log = logging.getLogger(__name__)

ACCOUNT_KEY_SECRET: Final = "acme:account-key"  # noqa: S105 - a vault entry name, not a secret
STATE_KEY: Final = "acme.state"  # account URIs per directory, alert de-duplication
# A certificate is renewed when fewer than this many days remain (Let's Encrypt: 90-day certificates).
RENEW_BEFORE: Final = timedelta(days=30)
# Nodes get a certificate only while it is valid at least this much longer (a renewal is due long
# before; this keeps an almost-expired certificate off a node that would serve it past its end).
SERVE_MARGIN: Final = timedelta(hours=1)


def chain_secret(domain: str) -> str:
    return f"acme:cert:{domain}:chain"


def key_secret(domain: str) -> str:
    return f"acme:cert:{domain}:key"


@dataclass(frozen=True)
class CertRow:
    domain: str
    zone_id: str | None
    not_before: datetime | None
    not_after: datetime | None
    serial_hex: str | None
    fingerprint_sha256: str | None
    issued_at: datetime | None
    last_attempt_at: datetime | None
    attempts: int
    retry_at: datetime | None
    last_error: str | None


_COLS = (
    AcmeCertificate.domain,
    AcmeCertificate.zone_id,
    AcmeCertificate.not_before,
    AcmeCertificate.not_after,
    AcmeCertificate.serial_hex,
    AcmeCertificate.fingerprint_sha256,
    AcmeCertificate.issued_at,
    AcmeCertificate.last_attempt_at,
    AcmeCertificate.attempts,
    AcmeCertificate.retry_at,
    AcmeCertificate.last_error,
)


async def cert_row(db: Db, domain: str) -> CertRow | None:
    r = (await db.execute(select(*_COLS).where(AcmeCertificate.domain == domain))).first()
    return CertRow(*r) if r is not None else None


async def cert_rows(db: Db) -> list[CertRow]:
    return [CertRow(*r) for r in (await db.execute(select(*_COLS).order_by(AcmeCertificate.domain))).all()]


def valid_now(row: CertRow | None, now: datetime) -> bool:
    return (
        row is not None
        and row.not_before is not None
        and row.not_after is not None
        and row.not_before <= now
        and now + SERVE_MARGIN < row.not_after
    )


def renewal_due(row: CertRow | None, now: datetime) -> bool:
    """No certificate yet, or fewer than :data:`RENEW_BEFORE` left."""
    return row is None or row.not_after is None or row.not_after - now < RENEW_BEFORE


@dataclass(frozen=True)
class Material:
    """A certificate a node may serve now: chain PEM (public) and its private key."""

    domain: str
    chain_pem: str
    key_pem: SecretValue = field(repr=False)
    not_after: datetime
    fingerprint_sha256: str


async def material(db: Db, domain: str | None, now: datetime, vault: Vault | None = None) -> Material | None:
    """The domain's certificate with its key if it is valid now, else None (also when the vault
    cannot open it: then nodes get no TLS inbound rather than a broken one)."""
    if domain is None:
        return None
    row = await cert_row(db, domain)
    if row is None or not valid_now(row, now) or row.not_after is None or row.fingerprint_sha256 is None:
        return None
    try:
        v = vault or Vault.from_config()
        chain = await v.get(db, chain_secret(domain))
        key = await v.get(db, key_secret(domain))
    except VaultError as exc:
        log.error("acme: the certificate of %s is unusable (%s)", domain, type(exc).__name__)
        return None
    if chain is None or key is None:
        return None
    return Material(
        domain=domain,
        chain_pem=chain.reveal(),
        key_pem=key,
        not_after=row.not_after,
        fingerprint_sha256=row.fingerprint_sha256,
    )


async def save_issued(
    db: Db,
    vault: Vault,
    *,
    domain: str,
    zone_id: str,
    chain_pem: str,
    key_pem: SecretValue,
    not_before: datetime,
    not_after: datetime,
    serial_hex: str,
    fingerprint_sha256: str,
    now: datetime,
) -> None:
    """Chain and key into the vault, facts into the row; the failure count starts over."""
    await vault.put(db, chain_secret(domain), chain_pem)
    await vault.put(db, key_secret(domain), key_pem)
    values: dict[str, Any] = {
        "zone_id": zone_id,
        "not_before": not_before,
        "not_after": not_after,
        "serial_hex": serial_hex,
        "fingerprint_sha256": fingerprint_sha256,
        "issued_at": now,
        "attempts": 0,
        "retry_at": None,
        "last_error": None,
        "updated_at": now,
    }
    await db.execute(
        insert(AcmeCertificate)
        .values(domain=domain, **values)
        .on_conflict_do_update(index_elements=[AcmeCertificate.domain], set_=values)
    )


async def mark_attempt(db: Db, *, domain: str, zone_id: str, now: datetime) -> None:
    values: dict[str, Any] = {"zone_id": zone_id, "last_attempt_at": now, "updated_at": now}
    await db.execute(
        insert(AcmeCertificate)
        .values(domain=domain, attempts=0, **values)
        .on_conflict_do_update(index_elements=[AcmeCertificate.domain], set_=values)
    )


def backoff(attempts: int) -> timedelta:
    """Wait after the n-th failed order in a row: 1 h, 2 h, 4 h, ... at most a day (Let's Encrypt
    allows 5 failed validations per hostname and hour; this stays far below)."""
    return timedelta(hours=min(24, 1 << min(5, max(0, attempts - 1))))


async def record_failure(
    db: Db, *, domain: str, code: str, now: datetime, retry_after: datetime | None = None
) -> datetime:
    """Count the failure and set when the next order may start. Returns that time."""
    row = await cert_row(db, domain)
    attempts = (row.attempts if row else 0) + 1
    retry_at = max(now + backoff(attempts), retry_after or now)
    values: dict[str, Any] = {
        "attempts": attempts,
        "retry_at": retry_at,
        "last_error": code[:60],
        "updated_at": now,
    }
    await db.execute(
        insert(AcmeCertificate)
        .values(domain=domain, **values)
        .on_conflict_do_update(index_elements=[AcmeCertificate.domain], set_=values)
    )
    return retry_at


async def forget(db: Db, domain: str) -> bool:
    """Remove a domain's certificate (vault entries and row). True if anything was there."""
    a = await delete_secret(db, chain_secret(domain))
    b = await delete_secret(db, key_secret(domain))
    gone = await db.execute(
        delete(AcmeCertificate).where(AcmeCertificate.domain == domain).returning(AcmeCertificate.domain)
    )
    return a or b or gone.first() is not None


async def load_state(db: Db) -> dict[str, Any]:
    return await get_setting(db, STATE_KEY) or {}


async def save_state(db: Db, state: dict[str, Any]) -> None:
    await put_setting(db, STATE_KEY, state)
