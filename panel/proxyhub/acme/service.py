"""One certificate order end to end (DNS-01), and what the control loop decides around it.

The order, step by step:

1. The account key comes from the vault (made and stored on first use); the account's URI at this
   directory is remembered in ``settings['acme.state']`` (not a secret).
2. A new order for ``*.<domain>`` with a fresh certificate key (ECDSA P-256).
3. Each DNS-01 challenge becomes a TXT record ``_acme-challenge.<domain>``, created through the same
   planner and apply path as every record the panel writes (purpose ``acme``, managed-record rule).
4. Every DoH resolver must answer the record with the expected value before the CA is asked to
   validate (no wasted failed validation, which Let's Encrypt rate-limits).
5. Validation, finalization with the CSR, the chain fetched.
6. Chain and key into the vault, facts into ``acme_certificates``, one actions_log row.
7. The TXT records are removed again, whatever happened (and any left over by a crash are removed
   at the start of the next run).

Every ACME call runs in a worker thread (the library is synchronous). Errors are short codes.
"""

from __future__ import annotations

import asyncio
import html
import logging
import ssl
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.acme.doh import DEFAULT_RESOLVERS, DohResolver, wait_for_txt
from proxyhub.acme.protocol import (
    LETS_ENCRYPT,
    AcmeError,
    AcmeSession,
    Challenge,
    Issued,
    directory_label,
    new_private_key_pem,
)
from proxyhub.acme.store import (
    ACCOUNT_KEY_SECRET,
    cert_row,
    load_state,
    mark_attempt,
    record_failure,
    save_issued,
    save_state,
    valid_now,
)
from proxyhub.cloudflare.client import CloudflareClient, CloudflareError
from proxyhub.cloudflare.records import SLOT_TTL, Desired
from proxyhub.cloudflare.service import reconcile_zone
from proxyhub.config import get_config
from proxyhub.fleet.actions import iso, log_action
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.vault import SecretValue, Vault

log = logging.getLogger(__name__)

ACTOR: Final = "acme"
PURPOSE: Final = "acme"
MAX_DNS_CALLS: Final = 6
ALERT_REPEAT: Final = timedelta(days=1)
ALERT_WHEN_LEFT: Final = timedelta(days=14)
RATE_LIMITED_WAIT: Final = timedelta(hours=24)


@dataclass(frozen=True)
class AcmeOptions:
    """Where certificates come from. Production: Let's Encrypt and the public DoH resolvers;
    ``PROXYHUB_ACME_DIRECTORY_URL`` / ``PROXYHUB_ACME_CA_FILE`` point it elsewhere (staging, or a
    test CA such as Pebble)."""

    directory_url: str = LETS_ENCRYPT
    ca_file: str | None = None
    resolvers: tuple[str, ...] = DEFAULT_RESOLVERS
    doh_verify: ssl.SSLContext | bool = field(default=True, compare=False)
    propagation_timeout_s: float = 300.0
    propagation_interval_s: float = 5.0
    finalize_timeout: timedelta = timedelta(minutes=3)

    @classmethod
    def from_config(cls) -> AcmeOptions:
        cfg = get_config()
        return cls(
            directory_url=cfg.acme_directory_url,
            ca_file=str(cfg.acme_ca_file) if cfg.acme_ca_file else None,
        )


SessionFactory = Callable[..., AcmeSession]


async def account_key(engine: AsyncEngine, vault: Vault) -> SecretValue:
    """The ACME account key from the vault, made and stored on first use."""
    async with engine.begin() as db:
        key = await vault.get(db, ACCOUNT_KEY_SECRET)
        if key is None:
            key = new_private_key_pem()
            await vault.put(db, ACCOUNT_KEY_SECRET, key)
    return key


async def _remember_account(engine: AsyncEngine, directory_url: str, uri: str) -> None:
    async with engine.begin() as db:
        state = await load_state(db)
        accounts = dict(state.get("accounts") or {})
        accounts[directory_label(directory_url)] = uri
        state["accounts"] = accounts
        await save_state(db, state)


async def _account_uri(engine: AsyncEngine, directory_url: str) -> str | None:
    async with engine.connect() as db:
        uri = ((await load_state(db)).get("accounts") or {}).get(directory_label(directory_url))
    return uri if isinstance(uri, str) else None


async def clear_challenges(engine: AsyncEngine, cf: CloudflareClient, zone_id: str) -> bool:
    """Remove every ``_acme-challenge`` record of ours from the zone. True if nothing is left."""
    try:
        report = await reconcile_zone(
            engine, cf, zone_id, [], purpose=PURPOSE, actor=ACTOR, max_api_calls=MAX_DNS_CALLS
        )
    except CloudflareError as exc:
        log.warning("acme: challenge records of zone %s not removed yet: %s", zone_id, exc)
        return False
    return not report.failed and not report.stopped


class ChallengeDns(Protocol):
    """Where the ``_acme-challenge`` TXT records go: the zone's real DNS."""

    async def publish(self, challenges: Sequence[Challenge]) -> None:
        """Create (or replace) the records; raises :class:`AcmeError` when they could not be."""

    async def clear(self) -> bool:
        """Remove every challenge record again; True if nothing is left."""


class CloudflareChallengeDns:
    """The production :class:`ChallengeDns`: the active zone in Cloudflare, through the same planner
    and apply path as every other record of the panel (purpose ``acme``): a record is created with
    our marker and our row, and only such a record is ever changed or deleted."""

    def __init__(self, engine: AsyncEngine, cf: CloudflareClient, zone_id: str) -> None:
        self._engine, self._cf, self._zone_id = engine, cf, zone_id

    async def publish(self, challenges: Sequence[Challenge]) -> None:
        desired = [
            Desired(name=c.record_name, type="TXT", content=c.value, ttl=SLOT_TTL, purpose=PURPOSE)
            for c in challenges
        ]
        report = await reconcile_zone(
            self._engine,
            self._cf,
            self._zone_id,
            desired,
            purpose=PURPOSE,
            actor=ACTOR,
            max_api_calls=MAX_DNS_CALLS,
        )
        if report.conflicts:
            raise AcmeError("dns_conflict")
        if report.failed or report.stopped:
            raise AcmeError("dns_publish_failed")

    async def clear(self) -> bool:
        return await clear_challenges(self._engine, self._cf, self._zone_id)


@dataclass(frozen=True)
class OrderOutcome:
    domain: str
    issued: Issued | None
    error: str | None = None
    retry_at: datetime | None = None


async def order_certificate(
    engine: AsyncEngine,
    dns: ChallengeDns,
    *,
    zone_id: str,
    domain: str,
    options: AcmeOptions,
    vault: Vault,
    now: datetime,
    session_factory: SessionFactory = AcmeSession,
) -> OrderOutcome:
    """Order ``*.<domain>`` now. Never raises for an ACME, DNS or network failure: the outcome
    carries the code, the failure is counted (backoff) and logged."""
    async with engine.begin() as db:
        await mark_attempt(db, domain=domain, zone_id=zone_id, now=now)
    try:
        issued = await _order(
            engine, dns, domain=domain, options=options, vault=vault, factory=session_factory
        )
    except AcmeError as exc:
        return await _failed(engine, domain, exc.code, now, exc.retry_after)
    except CloudflareError as exc:
        return await _failed(engine, domain, f"dns_{exc.kind}", now, None)
    except Exception as exc:  # anything else still counts, so the CA is never asked in a loop
        log.exception("acme: unexpected failure ordering *.%s", domain)
        return await _failed(engine, domain, f"unexpected_{type(exc).__name__}", now, None)
    async with engine.begin() as db:
        await save_issued(
            db,
            vault,
            domain=domain,
            zone_id=zone_id,
            chain_pem=issued.chain_pem,
            key_pem=issued.key_pem,
            not_before=issued.not_before,
            not_after=issued.not_after,
            serial_hex=issued.serial_hex,
            fingerprint_sha256=issued.fingerprint_sha256,
            now=now,
        )
        await log_action(
            db,
            actor=ACTOR,
            action="acme_certificate_issued",
            target=f"*.{domain}",
            dry_run=False,
            allowed=True,
            reasons=["dns-01"],
            context={
                "not_after": iso(issued.not_after),
                "serial": issued.serial_hex,
                "sha256": issued.fingerprint_sha256,
                "directory": options.directory_url,
            },
            result="certificate issued; chain and key stored in the vault",
        )
        state = await load_state(db)
        if state.pop("alert_at", None) is not None:
            await save_state(db, state)
    return OrderOutcome(domain=domain, issued=issued)


async def _order(
    engine: AsyncEngine,
    dns: ChallengeDns,
    *,
    domain: str,
    options: AcmeOptions,
    vault: Vault,
    factory: SessionFactory,
) -> Issued:
    key = await account_key(engine, vault)
    uri = await _account_uri(engine, options.directory_url)
    session = await asyncio.to_thread(
        factory, options.directory_url, key, account_uri=uri, ca_file=options.ca_file
    )
    try:
        if uri is None:
            uri = await asyncio.to_thread(session.register)
            await _remember_account(engine, options.directory_url, uri)
        order = await asyncio.to_thread(session.new_order, domain)
        published = False
        try:
            if order.challenges:
                names = {c.record_name for c in order.challenges}
                if len(names) != len(order.challenges):
                    raise AcmeError("acme_two_values_one_name")  # only *.<domain> is ordered
                published = True
                await dns.publish(order.challenges)
                async with AsyncExitStack() as stack:
                    resolvers = [
                        await stack.enter_async_context(DohResolver(url, verify=options.doh_verify))
                        for url in options.resolvers
                    ]
                    for c in order.challenges:
                        seen = await wait_for_txt(
                            resolvers,
                            c.record_name,
                            c.value,
                            timeout_s=options.propagation_timeout_s,
                            interval_s=options.propagation_interval_s,
                        )
                        if not seen:
                            raise AcmeError("dns_not_propagated")
            return await asyncio.to_thread(session.finalize, order, timeout=options.finalize_timeout)
        finally:
            if published:
                await dns.clear()
    finally:
        await asyncio.to_thread(session.close)


def failure_alert_text(domain: str, code: str, retry_at: datetime) -> str:
    return (
        f"⚠️ <b>گواهی TLS</b>: گرفتن گواهی <code>*.{html.escape(domain)}</code> از Let's Encrypt "
        f"نشد (<code>{html.escape(code)}</code>). تا گواهی گرفته نشود، nodeها کانفیگ تازه‌ی "
        "WebSocket + TLS نمیگیرند. پنل خودش دوباره تلاش میکند "
        f"(<code>{html.escape(iso(retry_at) or '')}</code>)."
    )


async def _failed(
    engine: AsyncEngine, domain: str, code: str, now: datetime, retry_after: datetime | None
) -> OrderOutcome:
    if code == "acme_rateLimited":  # Let's Encrypt's limits run over hours to a week: wait a day
        retry_after = max(retry_after or now, now + RATE_LIMITED_WAIT)
    async with engine.begin() as db:
        before = await cert_row(db, domain)
        retry_at = await record_failure(db, domain=domain, code=code, now=now, retry_after=retry_after)
        await log_action(
            db,
            actor=ACTOR,
            action="acme_order_failed",
            target=f"*.{domain}",
            dry_run=False,
            allowed=True,
            reasons=[code],
            context={"retry_at": iso(retry_at)},
            result="no certificate this time; the next order waits (backoff)",
        )
        # The owner hears of it only while the nodes are (or soon will be) without a certificate: a
        # renewal that fails with weeks left is retried quietly.
        urgent = not valid_now(before, now) or (
            before is not None and before.not_after is not None and before.not_after - now < ALERT_WHEN_LEFT
        )
        state = await load_state(db)
        last = state.get("alert_at")
        if urgent and not (isinstance(last, str) and now - datetime.fromisoformat(last) < ALERT_REPEAT):
            await enqueue_owner_alert(db, failure_alert_text(domain, code, retry_at))
            state["alert_at"] = now.isoformat()
            await save_state(db, state)
    log.warning("acme: order for *.%s failed (%s); next try at %s", domain, code, iso(retry_at))
    return OrderOutcome(domain=domain, issued=None, error=code, retry_at=retry_at)
