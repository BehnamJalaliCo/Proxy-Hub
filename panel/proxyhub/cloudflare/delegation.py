"""Lost domain registration or delegation (team fleet, round 3).

A pool domain is useless the moment its registration lapses (the registry removes it: NXDOMAIN) or
its name servers stop pointing at our Cloudflare account (moved to another provider, or to another
Cloudflare account after someone else registered it). Cloudflare notices that only slowly, so the
panel looks itself:

* **Check.** Every pool domain (active and standby) is looked up hourly with a DNS-over-HTTPS NS
  query at Cloudflare's public resolver (1.1.1.1, JSON API at ``cloudflare-dns.com/dns-query``).
  After a bad answer it is looked up again 30 minutes later.
* **Result** of one lookup: ``ok`` (every name server is ``*.ns.cloudflare.com`` and, once known,
  the same set as before), ``nxdomain``, ``not_cloudflare``, ``ns_changed`` (lost), or
  ``no_answer``, ``servfail``, ``error`` (inconclusive: they never decide anything).
* **Verdict** ``lost`` only when the two latest conclusive lookups since the domain's last pool
  change are both lost, at least 30 minutes apart, and both younger than 6 hours.
* **Action** on ``lost``: the domain leaves rotation like a filtered one
  (proxyhub/cloudflare/rotation.py ``take_out``; reason ``delegation_lost``): an active domain is
  replaced by the next standby in the same transaction, the owner gets a Persian alert. It moves to
  ``none`` (NOT ``filtered``): it is never deleted from Cloudflare for this, because renewing the
  domain makes it whole again. It keeps being checked, and once two lookups 30 minutes apart are
  ``ok`` again, the panel puts it back as ``standby`` by itself (unless the owner moved it meanwhile).

Only public data is stored: the zone's name-server host names. Nothing about any user (rule 7);
no token is involved (the resolver is public).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Literal, Self

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from proxyhub.agent_api.validation import valid_domain
from proxyhub.cloudflare.pool import PoolState
from proxyhub.cloudflare.rotation import GONE, NO_STANDBY_FA, PoolZone, load_zones, move, take_out
from proxyhub.db.models_fleet import DelegationCheck, DelegationState
from proxyhub.fleet.actions import log_action
from proxyhub.reconcilers.base import Reconciler
from proxyhub.telegram.store import enqueue_owner_alert

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
P = PoolState

ACTOR = "domains"
# Cloudflare's public resolver (1.1.1.1), its DNS-over-HTTPS JSON API.
DOH_URL = "https://cloudflare-dns.com/dns-query"
USER_AGENT = "proxy-hub-panel"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
CHECK_EVERY = timedelta(hours=1)
RECHECK_AFTER = timedelta(minutes=30)
MIN_GAP = timedelta(minutes=30)
MAX_AGE = timedelta(hours=6)
RETENTION = timedelta(days=30)
MAX_CHECKS_PER_TICK = 6
CF_NS_SUFFIX = ".ns.cloudflare.com"
DNS_TYPE_NS = 2

Result = Literal["ok", "nxdomain", "not_cloudflare", "ns_changed", "no_answer", "servfail", "error"]
Verdict = Literal["ok", "lost", "unknown"]
LOST: frozenset[str] = frozenset({"nxdomain", "not_cloudflare", "ns_changed"})
CONCLUSIVE: frozenset[str] = LOST | {"ok"}
CHECKED_STATES = (P.ACTIVE.value, P.STANDBY.value)


# --- one lookup: parsing and classification (pure) --------------------------------------------


@dataclass(frozen=True)
class NsAnswer:
    """A DoH answer reduced to what matters: the DNS status (RCODE) and the zone's NS host names
    (lower case, no trailing dot). ``error`` is a short code when no usable answer came back."""

    rcode: int | None
    nameservers: tuple[str, ...] = ()
    error: str | None = None


def _host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().rstrip(".").lower()
    return name if name and valid_domain(name) else None


def parse_doh(raw: Any, zone: str) -> NsAnswer:
    """The JSON a DoH resolver returns for ``?name=<zone>&type=NS`` (RFC 8427-like shape)."""
    if not isinstance(raw, Mapping):
        return NsAnswer(None, error="invalid_response")
    status = raw.get("Status")
    if not isinstance(status, int) or isinstance(status, bool):
        return NsAnswer(None, error="invalid_response")
    answers = raw.get("Answer")
    found: set[str] = set()
    if isinstance(answers, list):
        apex = zone.rstrip(".").lower()
        for a in answers:
            if not isinstance(a, Mapping) or a.get("type") != DNS_TYPE_NS:
                continue
            if _host(a.get("name")) != apex:
                continue  # an NS record of another name (never expected for the apex query)
            host = _host(a.get("data"))
            if host is not None:
                found.add(host)
    return NsAnswer(status, tuple(sorted(found)))


def classify(answer: NsAnswer, baseline: Sequence[str] | None) -> Result:
    if answer.error is not None or answer.rcode is None:
        return "error"
    if answer.rcode == 3:
        return "nxdomain"
    if answer.rcode == 2:
        return "servfail"
    if answer.rcode != 0:
        return "error"
    if not answer.nameservers:
        return "no_answer"
    if not all(ns.endswith(CF_NS_SUFFIX) for ns in answer.nameservers):
        return "not_cloudflare"
    if baseline and set(answer.nameservers) != set(baseline):
        return "ns_changed"
    return "ok"


# --- verdict (pure) ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationRound:
    at: datetime
    result: str


def _conclusive(rounds: Sequence[DelegationRound]) -> list[DelegationRound]:
    return sorted((r for r in rounds if r.result in CONCLUSIVE), key=lambda r: r.at, reverse=True)


def delegation_verdict(rounds: Sequence[DelegationRound], now: datetime) -> tuple[Verdict, list[str]]:
    """``lost`` needs two lost lookups in a row (inconclusive ones skipped), >= 30 min apart and
    both younger than 6 h; ``ok`` when the latest conclusive lookup is ok; else ``unknown``."""
    rows = _conclusive(rounds)
    if not rows:
        return "unknown", ["no_conclusive_lookup"]
    last = rows[0]
    if now - last.at > MAX_AGE:
        return "unknown", ["lookups_too_old"]
    if last.result == "ok":
        return "ok", ["delegated_to_cloudflare"]
    if len(rows) < 2:
        return "unknown", ["single_lookup", last.result]
    prev = rows[1]
    if prev.result == "ok":
        return "unknown", ["not_confirmed", last.result]
    if last.at - prev.at < MIN_GAP:
        return "unknown", ["lookups_too_close", last.result]
    if now - prev.at > MAX_AGE:
        return "unknown", ["lookups_too_old", last.result]
    return "lost", [last.result, prev.result]


def delegation_restored(rounds: Sequence[DelegationRound], now: datetime) -> bool:
    """Two ``ok`` lookups in a row, >= 30 min apart, both younger than 6 h."""
    rows = _conclusive(rounds)
    if len(rows) < 2:
        return False
    last, prev = rows[0], rows[1]
    return (
        last.result == "ok"
        and prev.result == "ok"
        and last.at - prev.at >= MIN_GAP
        and now - prev.at <= MAX_AGE
    )


def check_due(last_at: datetime | None, last_result: str | None, now: datetime) -> bool:
    if last_at is None:
        return True
    return now - last_at >= (CHECK_EVERY if last_result == "ok" else RECHECK_AFTER)


# --- the resolver (network I/O only) ------------------------------------------------------------


class DohClient:
    """DNS-over-HTTPS JSON lookups at Cloudflare's public resolver. No token, no user data."""

    def __init__(self, url: str | None = None, *, timeout: httpx.Timeout = TIMEOUT) -> None:
        self._url = url or DOH_URL
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"accept": "application/dns-json", "user-agent": USER_AGENT},
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._client.aclose()

    async def ns(self, zone: str) -> NsAnswer:
        if not valid_domain(zone):
            return NsAnswer(None, error="invalid_name")
        try:
            resp = await self._client.get(self._url, params={"name": zone, "type": "NS"})
        except httpx.TimeoutException:
            return NsAnswer(None, error="timeout")
        except httpx.HTTPError:
            return NsAnswer(None, error="unreachable")
        if resp.status_code != 200:
            return NsAnswer(None, error=f"http_{resp.status_code}")
        try:
            raw = resp.json()
        except ValueError:
            return NsAnswer(None, error="invalid_response")
        return parse_doh(raw, zone)


# --- storage and action ------------------------------------------------------------------------


@dataclass(frozen=True)
class Due:
    zone: PoolZone
    rotated_out: bool


async def _rounds(db: Db, zone_id: str, since: datetime | None, limit: int = 10) -> list[DelegationRound]:
    stmt = select(DelegationCheck.at, DelegationCheck.result).where(DelegationCheck.zone_id == zone_id)
    if since is not None:
        stmt = stmt.where(DelegationCheck.at >= since)
    rows = (await db.execute(stmt.order_by(DelegationCheck.at.desc()).limit(limit))).all()
    return [DelegationRound(r.at, r.result) for r in rows]


async def _last(db: Db, zone_id: str) -> tuple[datetime | None, str | None, str | None]:
    row = (
        await db.execute(
            select(DelegationCheck.at, DelegationCheck.result, DelegationCheck.verdict)
            .where(DelegationCheck.zone_id == zone_id)
            .order_by(DelegationCheck.at.desc(), DelegationCheck.id.desc())
            .limit(1)
        )
    ).first()
    return (row.at, row.result, row.verdict) if row is not None else (None, None, None)


def _rotated_out(z: PoolZone, rotated_out_at: datetime | None) -> bool:
    """Taken out by this module and not moved by anyone since."""
    return (
        rotated_out_at is not None
        and z.pool_state == P.NONE.value
        and z.pool_changed_at is not None
        and z.pool_changed_at == rotated_out_at
    )


async def due_zones(db: Db, now: datetime) -> list[Due]:
    """Pool domains due for a lookup (the active one first), and domains this module took out."""
    rotated = {
        r.zone_id: r.rotated_out_at
        for r in (await db.execute(select(DelegationState.zone_id, DelegationState.rotated_out_at))).all()
    }
    due: list[tuple[tuple[int, datetime], Due]] = []
    for z in await load_zones(db):
        if z.status == GONE:
            continue
        out = _rotated_out(z, rotated.get(z.id))
        if z.pool_state not in CHECKED_STATES and not out:
            continue
        last_at, last_result, _ = await _last(db, z.id)
        if check_due(last_at, last_result, now):
            due.append(((0 if z.pool_state == P.ACTIVE.value else 1, last_at or now), Due(z, out)))
    due.sort(key=lambda d: d[0])
    return [d for _, d in due]


@dataclass(frozen=True)
class Outcome:
    result: str
    verdict: Verdict
    taken_out: bool = False
    restored: bool = False
    replacement: str | None = None


async def _upsert_state(db: Db, zone_id: str, now: datetime, **values: Any) -> None:
    stmt = insert(DelegationState).values(zone_id=zone_id, updated_at=now, **values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[DelegationState.zone_id], set_={"updated_at": now, **values}
        )
    )


async def record_lookup(
    db: Db, zone_id: str, answer: NsAnswer, now: datetime, at: datetime
) -> Outcome | None:
    """Store one lookup, recompute the verdict and act on it, in the caller's transaction (every zone
    row locked, as every pool decision). None if the zone is gone."""
    zone = next((z for z in await load_zones(db, lock=True) if z.id == zone_id), None)
    if zone is None:
        return None
    st = (
        await db.execute(
            select(DelegationState.baseline_ns, DelegationState.rotated_out_at, DelegationState.rotated_from)
            .where(DelegationState.zone_id == zone_id)
            .with_for_update()
        )
    ).first()
    baseline = list(st.baseline_ns) if st is not None and st.baseline_ns else None
    rotated_out_at = st.rotated_out_at if st is not None else None
    out = _rotated_out(zone, rotated_out_at)
    result = classify(answer, baseline)
    if result == "ok" and baseline is None and zone.status == "active":
        await _upsert_state(db, zone.id, now, baseline_ns=list(answer.nameservers), baseline_at=at)
    since = zone.pool_changed_at
    rounds = [DelegationRound(at, result), *await _rounds(db, zone.id, since)]
    if since is not None and at < since:
        rounds = rounds[1:]
    verdict, reasons = delegation_verdict(rounds, now)
    _, _, previous = await _last(db, zone.id)
    await db.execute(
        insert(DelegationCheck).values(
            zone_id=zone.id,
            at=at,
            pool_state=zone.pool_state,
            result=result,
            rcode=answer.rcode,
            nameservers=list(answer.nameservers)[:13],
            verdict=verdict,
        )
    )
    if (previous or "unknown") != verdict:
        await log_action(
            db,
            actor=ACTOR,
            action="domain_delegation",
            target=zone.name,
            dry_run=False,
            allowed=True,
            reasons=[verdict, *reasons],
            context={
                "from": previous or "unknown",
                "to": verdict,
                "result": result,
                "error": answer.error,
                "nameservers": list(answer.nameservers)[:13],
                "pool_state": zone.pool_state,
            },
            result=f"NS lookup of {zone.name}: {result} -> delegation {verdict}",
        )
    if verdict == "lost" and zone.pool_state in CHECKED_STATES:
        return await _lost(db, zone, now, result, reasons)
    if out and delegation_restored(rounds, now):
        return await _restore(db, zone, now, result, verdict)
    return Outcome(result, verdict)


async def _lost_zone_ids(db: Db, now: datetime) -> list[str]:
    """Zones whose latest lookup already said lost (never promoted in place of another)."""
    rows = (
        await db.execute(
            select(DelegationCheck.zone_id, DelegationCheck.verdict)
            .where(DelegationCheck.at >= now - MAX_AGE)
            .order_by(DelegationCheck.at.desc(), DelegationCheck.id.desc())
        )
    ).all()
    latest: dict[str, str] = {}
    for r in rows:
        latest.setdefault(r.zone_id, r.verdict)
    return [zone_id for zone_id, verdict in latest.items() if verdict == "lost"]


async def _lost(db: Db, zone: PoolZone, now: datetime, result: str, reasons: list[str]) -> Outcome:
    exclude = await _lost_zone_ids(db, now)
    done = await take_out(db, zone.id, P.NONE, now, "delegation_lost", exclude=exclude, result=result)
    if done is None:  # pragma: no cover - checked under the same lock just before
        return Outcome(result, "lost")
    # take_out moved the zone at ``now`` (its pool_changed_at): remember that it was us, so the zone
    # comes back by itself once its delegation is good again (unless someone moves it meanwhile).
    await _upsert_state(db, zone.id, now, rotated_out_at=now, rotated_from=zone.pool_state)
    await enqueue_owner_alert(
        db, lost_alert_fa(zone.name, result, done.was_active, done.replacement, done.standby_left)
    )
    return Outcome(result, "lost", taken_out=True, replacement=done.replacement)


async def _restore(db: Db, zone: PoolZone, now: datetime, result: str, verdict: Verdict) -> Outcome:
    await move(db, zone, P.STANDBY, now, "delegation_restored")
    await _upsert_state(db, zone.id, now, rotated_out_at=None, rotated_from=None)
    await enqueue_owner_alert(
        db,
        f"✅ دامنه‌ی <b>{html.escape(zone.name)}</b> دوباره روی Cloudflare است و به دامنه‌های "
        "ذخیره (آماده‌به‌کار) برگشت.",
    )
    return Outcome(result, verdict, restored=True)


WHY_FA: dict[str, str] = {
    "nxdomain": "در اینترنت پیدا نمیشود (NXDOMAIN)؛ احتمالاً ثبت آن تمام شده یا تمدید نشده است",
    "not_cloudflare": "nameserverهایش دیگر Cloudflare نیستند",
    "ns_changed": "nameserverهایش به Cloudflare دیگری (حساب دیگری) رفته‌اند",
}


def lost_alert_fa(name: str, result: str, was_active: bool, replacement: str | None, left: int) -> str:
    n = f"<b>{html.escape(name)}</b>"
    why = WHY_FA.get(result, "دیگر روی Cloudflare نیست")
    if was_active and replacement:
        head = f"⚠️ دامنه‌ی {n} {why}. از دور خارج شد و <b>{html.escape(replacement)}</b> جایش فعال شد."
    elif was_active:
        head = (
            f"⚠️ دامنه‌ی {n} {why}. از دور خارج شد. دامنه‌ی فعال دیگری نیست؛ "
            "تا آمدن دامنه‌ی تازه، لینک‌های تازه با IP سرورها ساخته میشوند."
        )
    else:
        head = f"⚠️ دامنه‌ی ذخیره‌ی {n} {why} و از استخر دامنه بیرون رفت."
    tail = (
        "این دامنه از Cloudflare پاک نمیشود. اگر آن را تمدید یا درست کنید، پنل خودش آن را دوباره "
        "به ذخیره برمیگرداند."
    )
    text = f"{head}\n{tail}"
    if left == 0:
        text += "\n\n" + NO_STANDBY_FA
    return text


# --- the loop --------------------------------------------------------------------------------------

ClientFactory = Callable[[], DohClient]


async def check_delegations(
    engine: AsyncEngine, client_factory: ClientFactory = DohClient, *, max_checks: int = MAX_CHECKS_PER_TICK
) -> list[Outcome]:
    """Look up the due domains (network I/O outside any transaction), store and act on each in its
    own short transaction. With no pool domain it reads a few rows and calls nothing."""
    now = datetime.now(UTC)
    async with engine.connect() as db:
        due = await due_zones(db, now)
    if not due:
        return []
    outcomes: list[Outcome] = []
    async with client_factory() as doh:
        for item in due[:max_checks]:
            at = datetime.now(UTC)
            answer = await doh.ns(item.zone.name)
            async with engine.begin() as db:
                outcome = await record_lookup(db, item.zone.id, answer, datetime.now(UTC), at)
            if outcome is not None:
                outcomes.append(outcome)
                log.info("domains: NS of %s -> %s (%s)", item.zone.name, outcome.result, outcome.verdict)
    async with engine.begin() as db:
        await db.execute(delete(DelegationCheck).where(DelegationCheck.at < now - RETENTION))
    return outcomes


class DelegationReconciler(Reconciler):
    """Every minute: the pool domains that are due get their NS lookup (hourly each; 30 minutes after
    a bad answer). Its own loop and advisory lock, next to the domains loop's filter checks; both
    serialise their pool decisions on the same zone row locks."""

    name = "delegation"
    interval_seconds = 60.0

    def __init__(self, client_factory: ClientFactory = DohClient) -> None:
        self._client_factory = client_factory

    async def run_once(self, conn: AsyncConnection) -> None:
        # ``conn`` is never used, so its transaction never starts: no transaction is ever open
        # across the network lookups (each result is stored in its own short transaction).
        await check_delegations(conn.engine, self._client_factory)
