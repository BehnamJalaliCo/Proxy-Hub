"""The domain pool, run by the panel itself (decision D35, docs/10 section 3.10).

* **New domains join by themselves.** When zones are stored (a token is set, or the hourly
  refresh), a zone that is still ``none`` and was never moved (``pool_changed_at`` NULL) becomes
  ``standby`` once Cloudflare serves it (status ``active``) and DNS:Edit is confirmed. A pending
  zone (nameservers not on Cloudflare yet) waits for a later refresh. A zone the owner moved (to
  ``none``, ``retired``, ...) is never moved back automatically.
* **Always one active domain.** With no ``active`` zone, the usable standby zone the panel saw
  first becomes active (a standby whose latest check showed a hijacked answer comes last).
* **A filtered domain leaves rotation.** On a ``filtered`` verdict (proxyhub/cloudflare/filtering.py)
  the active zone becomes ``filtered`` and the next standby becomes active IN THE SAME
  TRANSACTION; a filtered standby simply leaves the pool. The DNS loop then moves the slot names by
  itself. The owner gets a Persian alert; with no standby left, a reminder to buy a domain,
  repeated at most once a day while the pool stays without one.

Every automatic move goes through the same state machine as the owner's (proxyhub/cloudflare/pool.py)
and is written to actions_log with actor ``domains``. Nothing here calls Cloudflare or Globalping.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.cloudflare.filtering import DomainRound, Verdict, domain_verdict, suspicious
from proxyhub.cloudflare.pool import SERVING_ZONE_STATUSES, PoolState, check_pool_transition
from proxyhub.db.models_net import CfState, CfZone, DomainCheck
from proxyhub.fleet.actions import iso, log_action
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.telegram.templates import fa_digits

Db = AsyncConnection | AsyncSession
P = PoolState

ACTOR: Final = "domains"
GONE: Final = "gone"
NO_STANDBY_REPEAT: Final = timedelta(days=1)
DELETE_GRACE: Final = timedelta(hours=24)  # see proxyhub/cloudflare/deletion.py
VERDICT_ROUNDS: Final = 10  # rounds read back for one verdict (only the latest 2 with answers decide)

NO_STANDBY_FA: Final = (
    "دامنه‌ی ذخیره‌ای نمانده؛ یک دامنه‌ی تازه بخرید و به حساب Cloudflare مخصوص Proxy-Hub اضافه کنید."
)


@dataclass(frozen=True)
class PoolZone:
    id: str
    name: str
    status: str
    pool_state: str
    dns_edit: bool | None
    pool_changed_at: datetime | None
    created_at: datetime
    filtered_at: datetime | None
    deleted_at: datetime | None
    delete_claimed_at: datetime | None
    delete_blocked: str | None
    delete_error: str | None
    delete_attempts: int
    delete_retry_at: datetime | None


_COLS = (
    CfZone.id,
    CfZone.name,
    CfZone.status,
    CfZone.pool_state,
    CfZone.dns_edit,
    CfZone.pool_changed_at,
    CfZone.created_at,
    CfZone.filtered_at,
    CfZone.deleted_at,
    CfZone.delete_claimed_at,
    CfZone.delete_blocked,
    CfZone.delete_error,
    CfZone.delete_attempts,
    CfZone.delete_retry_at,
)


def _zone(r: Any) -> PoolZone:
    return PoolZone(
        id=r.id,
        name=r.name,
        status=r.status,
        pool_state=r.pool_state,
        dns_edit=r.dns_edit,
        pool_changed_at=r.pool_changed_at,
        created_at=r.created_at,
        filtered_at=r.filtered_at,
        deleted_at=r.deleted_at,
        delete_claimed_at=r.delete_claimed_at,
        delete_blocked=r.delete_blocked,
        delete_error=r.delete_error,
        delete_attempts=int(r.delete_attempts or 0),
        delete_retry_at=r.delete_retry_at,
    )


async def load_zones(db: Db, *, lock: bool = False) -> list[PoolZone]:
    """Every zone, in id order. ``lock`` takes a row lock on all of them (same order everywhere, so
    two pool changes never deadlock): pool decisions are serialised with the owner's."""
    stmt = select(*_COLS).order_by(CfZone.id)
    if lock:
        stmt = stmt.with_for_update()
    return [_zone(r) for r in (await db.execute(stmt)).all()]


def usable_standby(z: PoolZone) -> bool:
    """A standby zone that could take over right now."""
    return z.pool_state == P.STANDBY.value and z.status in SERVING_ZONE_STATUSES and z.dns_edit is True


def joins_by_itself(z: PoolZone) -> bool:
    """Still ``none``, never moved by anyone, served by Cloudflare, DNS:Edit confirmed."""
    return (
        z.pool_state == P.NONE.value
        and z.pool_changed_at is None
        and z.status in SERVING_ZONE_STATUSES
        and z.dns_edit is True
    )


async def current_active_domain(db: Db) -> str | None:
    """The domain whose slot names go into new posts (for the content engine, M3.1), if any."""
    return (
        await db.execute(
            select(CfZone.name).where(CfZone.pool_state == P.ACTIVE.value, CfZone.status != GONE)
        )
    ).scalar_one_or_none()


# --- rounds ------------------------------------------------------------------------------------


def _round(r: Any) -> DomainRound:
    return DomainRound(
        at=r.at,
        status=r.status,
        probes_asked=r.probes_asked,
        probes=r.probes,
        answered=r.answered,
        hijacked=r.hijacked,
        networks_answered=r.networks_answered,
        networks_hijacked=r.networks_hijacked,
        ref=r.ref,
    )


_ROUND_COLS = (
    DomainCheck.at,
    DomainCheck.status,
    DomainCheck.probes_asked,
    DomainCheck.probes,
    DomainCheck.answered,
    DomainCheck.hijacked,
    DomainCheck.networks_answered,
    DomainCheck.networks_hijacked,
    DomainCheck.ref,
)


async def rounds_since(
    db: Db, zone_id: str, since: datetime | None, limit: int = VERDICT_ROUNDS
) -> list[DomainRound]:
    """The zone's latest rounds measured in its current pool state (newest first). A round measured
    before the last pool change (by the owner or the panel) never counts for the new state."""
    stmt = select(*_ROUND_COLS).where(DomainCheck.zone_id == zone_id)
    if since is not None:
        stmt = stmt.where(DomainCheck.at >= since)
    rows = (await db.execute(stmt.order_by(DomainCheck.at.desc()).limit(limit))).all()
    return [_round(r) for r in rows]


async def last_check_at(db: Db, zone_id: str) -> datetime | None:
    value = (
        await db.execute(select(func.max(DomainCheck.at)).where(DomainCheck.zone_id == zone_id))
    ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


async def is_suspicious(db: Db, z: PoolZone) -> bool:
    return suspicious(await rounds_since(db, z.id, z.pool_changed_at))


# --- moves -------------------------------------------------------------------------------------


async def move(db: Db, z: PoolZone, target: PoolState, now: datetime, why: str, **context: Any) -> None:
    """One automatic pool move, checked by the owner's state machine and logged."""
    current = PoolState(z.pool_state)
    if not check_pool_transition(current, target, zone_status=z.status, dns_edit=z.dns_edit):
        return
    values: dict[str, Any] = {"pool_state": target.value, "pool_changed_at": now, "updated_at": now}
    if target == P.FILTERED:
        values |= {
            "filtered_at": now,
            "delete_claimed_at": None,
            "delete_blocked": None,
            "delete_error": None,
            "delete_attempts": 0,
            "delete_retry_at": None,
        }
    elif current == P.FILTERED:
        values["filtered_at"] = None
    result = await db.execute(
        update(CfZone).where(CfZone.id == z.id, CfZone.pool_state == current.value).values(**values)
    )
    if getattr(result, "rowcount", 1) != 1:  # pragma: no cover - rows are locked by the caller
        raise RuntimeError("zone changed while locked")
    await log_pool_change(db, ACTOR, z.name, current, target, why, **context)


async def log_pool_change(
    db: Db, actor: str, name: str, old: PoolState, new: PoolState, why: str, **context: Any
) -> None:
    await log_action(
        db,
        actor=actor,
        action="cloudflare_pool_change",
        target=name,
        dry_run=False,
        allowed=True,
        reasons=[why],
        context={"from": old.value, "to": new.value, **context},
        result=f"{name}: {old.value} -> {new.value}",
    )


async def promote_standby(db: Db, zones: Sequence[PoolZone], now: datetime, why: str) -> PoolZone | None:
    """With no active zone: the usable standby first seen (ties: name, id) becomes active. A standby
    whose latest check showed a hijacked answer is taken only when nothing better is left."""
    if any(z.pool_state == P.ACTIVE.value for z in zones):
        return None
    candidates = [z for z in zones if usable_standby(z)]
    if not candidates:
        return None
    flagged = {z.id: await is_suspicious(db, z) for z in candidates}
    best = min(candidates, key=lambda z: (flagged[z.id], z.created_at, z.name, z.id))
    await move(db, best, P.ACTIVE, now, why)
    return best


@dataclass(frozen=True)
class PoolUpdate:
    joined: list[str]
    promoted: str | None


async def auto_pool(db: Db, now: datetime) -> PoolUpdate:
    """Run after zones were stored (token set, hourly refresh), in the same transaction."""
    zones = await load_zones(db, lock=True)
    joined: list[str] = []
    for z in zones:
        if joins_by_itself(z):
            await move(db, z, P.STANDBY, now, "auto_new_zone")
            joined.append(z.name)
    if joined:
        zones = await load_zones(db)
    promoted = await promote_standby(db, zones, now, "auto_promote")
    # The "no standby left" reminder streak (if any) ends in remind_no_standby once a usable
    # standby exists; a domain that joined and was promoted right away leaves the pool still empty.
    return PoolUpdate(joined=joined, promoted=promoted.name if promoted else None)


# --- one filter check: store, decide, act --------------------------------------------------------


@dataclass(frozen=True)
class RoundOutcome:
    verdict: Verdict
    reasons: list[str]
    filtered: bool = False
    replacement: str | None = None
    standby_left: int = 0


async def record_round(
    db: Db,
    zone_id: str,
    rnd: DomainRound,
    now: datetime,
) -> RoundOutcome | None:
    """Store one check, recompute the zone's verdict, and act on ``filtered`` (all in the caller's
    transaction, with every zone row locked). Returns None if the zone no longer exists."""
    zone = next((z for z in await load_zones(db, lock=True) if z.id == zone_id), None)
    if zone is None:
        return None
    previous = (
        await db.execute(
            select(DomainCheck.verdict)
            .where(DomainCheck.zone_id == zone.id)
            .order_by(DomainCheck.at.desc(), DomainCheck.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    since = zone.pool_changed_at
    stored = await rounds_since(db, zone.id, since)
    counts = [rnd] if since is None or rnd.at >= since else []
    verdict, reasons = domain_verdict([*counts, *stored], now)
    await db.execute(
        insert(DomainCheck).values(
            zone_id=zone.id,
            at=rnd.at,
            pool_state=zone.pool_state,
            status=rnd.status,
            ref=rnd.ref[:64] if rnd.ref else None,
            probes_asked=rnd.probes_asked,
            probes=rnd.probes,
            answered=rnd.answered,
            hijacked=rnd.hijacked,
            networks_answered=rnd.networks_answered,
            networks_hijacked=rnd.networks_hijacked,
            detail=rnd.detail_json(),
            verdict=verdict,
            verdict_reasons=reasons,
        )
    )
    evidence = {
        "answered": rnd.answered,
        "hijacked": rnd.hijacked,
        "networks_answered": rnd.networks_answered,
        "networks_hijacked": rnd.networks_hijacked,
    }
    if (previous or "unknown") != verdict:
        await log_action(
            db,
            actor=ACTOR,
            action="domain_verdict",
            target=zone.name,
            dry_run=False,
            allowed=True,
            reasons=[verdict, *reasons],
            context={"from": previous or "unknown", "to": verdict, "pool_state": zone.pool_state, **evidence},
            result=f"filter check from Iran: {previous or 'unknown'} -> {verdict}",
        )
    if verdict != "filtered" or zone.pool_state not in (P.ACTIVE.value, P.STANDBY.value):
        return RoundOutcome(verdict, reasons)
    return await _take_out(db, zone, now, evidence)


async def _take_out(db: Db, zone: PoolZone, now: datetime, evidence: dict[str, Any]) -> RoundOutcome:
    was_active = zone.pool_state == P.ACTIVE.value
    await move(db, zone, P.FILTERED, now, "domain_filtered", **evidence)
    replacement: PoolZone | None = None
    if was_active:
        others = [z for z in await load_zones(db) if z.id != zone.id]
        replacement = await promote_standby(db, others, now, "replaces_filtered")
    left = sum(1 for z in await load_zones(db) if usable_standby(z))
    text = filtered_alert_fa(
        zone.name, was_active=was_active, replacement=replacement.name if replacement else None
    )
    if left == 0:
        text += "\n\n" + NO_STANDBY_FA
        await _set_standby_alert(db, now)
    await enqueue_owner_alert(db, text)
    return RoundOutcome(
        "filtered",
        [],
        filtered=True,
        replacement=replacement.name if replacement else None,
        standby_left=left,
    )


def filtered_alert_fa(name: str, *, was_active: bool, replacement: str | None) -> str:
    """The Persian owner alert for a domain just taken out of rotation (Telegram HTML)."""
    n = f"<b>{html.escape(name)}</b>"
    if was_active and replacement:
        r = f"<b>{html.escape(replacement)}</b>"
        head = f"⚠️ دامنه‌ی {n} در ایران فیلتر شد و از دور خارج شد؛ {r} جایش فعال شد."
    elif was_active:
        head = f"⚠️ دامنه‌ی {n} در ایران فیلتر شد و از دور خارج شد."
    else:
        head = f"⚠️ دامنه‌ی ذخیره‌ی {n} در ایران فیلتر شد و از استخر دامنه بیرون رفت."
    hours = fa_digits(str(int(DELETE_GRACE.total_seconds() // 3600)))
    tail = (
        f"پس از {hours} ساعت از Cloudflare پاک میشود (اگر کلید توقف خاموش باشد). اگر فکر میکنید "
        "اشتباه است، تا آن موقع در داشبورد آن را به «آماده‌به‌کار» برگردانید."
    )
    return f"{head}\n{tail}"


# --- the "no standby left" reminder --------------------------------------------------------------


async def _set_standby_alert(db: Db, now: datetime) -> None:
    stmt = insert(CfState).values(id=1, standby_alert_at=now, updated_at=now)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[CfState.id], set_={"standby_alert_at": now, "updated_at": now}
        )
    )


async def _settle_standby_alert(db: Db, zones: Sequence[PoolZone], now: datetime) -> None:
    """A usable standby exists again: the reminder streak ends."""
    if any(usable_standby(z) for z in zones):
        await db.execute(
            update(CfState)
            .where(CfState.id == 1, CfState.standby_alert_at.is_not(None))
            .values(standby_alert_at=None, updated_at=now)
        )


async def remind_no_standby(db: Db, now: datetime) -> bool:
    """Repeat the "buy a domain" alert at most once a day while the pool has no usable standby
    (only after the pool ran out because a domain was filtered). Returns True if one was queued."""
    since = (await db.execute(select(CfState.standby_alert_at).where(CfState.id == 1))).scalar_one_or_none()
    if since is None:
        return False
    zones = await load_zones(db)
    if any(usable_standby(z) for z in zones):
        await _settle_standby_alert(db, zones, now)
        return False
    if now - since < NO_STANDBY_REPEAT:
        return False
    await _set_standby_alert(db, now)
    await enqueue_owner_alert(db, "⚠️ " + NO_STANDBY_FA)
    await log_action(
        db,
        actor=ACTOR,
        action="domain_no_standby",
        target=None,
        dry_run=False,
        allowed=False,
        reasons=["no_standby"],
        context={"since": iso(since)},
        result="no standby domain left; the owner was reminded to add one",
    )
    return True


# --- taking a domain out for another reason (round 3, team fleet) ---------------------------------


@dataclass(frozen=True)
class TakeOut:
    was_active: bool
    replacement: str | None
    standby_left: int


async def take_out(
    db: Db,
    zone_id: str,
    target: PoolState,
    now: datetime,
    why: str,
    *,
    exclude: Sequence[str] = (),
    **context: Any,
) -> TakeOut | None:
    """Take a pool domain out of rotation for a reason other than a filter verdict (its registration
    or delegation is lost: proxyhub/cloudflare/delegation.py), with the same steps as a filtered one:
    the next usable standby (none of ``exclude``) becomes active in the same transaction, and with no
    standby left the daily "buy a domain" reminder starts. The caller words and queues the alert.
    None if the zone is not active or standby (any more)."""
    zones = await load_zones(db, lock=True)
    zone = next((z for z in zones if z.id == zone_id), None)
    if zone is None or zone.pool_state not in (P.ACTIVE.value, P.STANDBY.value):
        return None
    was_active = zone.pool_state == P.ACTIVE.value
    await move(db, zone, target, now, why, **context)
    replacement: PoolZone | None = None
    if was_active:
        others = [z for z in await load_zones(db) if z.id != zone.id and z.id not in exclude]
        replacement = await promote_standby(db, others, now, f"replaces_{why}")
    left = sum(1 for z in await load_zones(db) if usable_standby(z) and z.id not in exclude)
    if left == 0:
        await _set_standby_alert(db, now)
    return TakeOut(was_active, replacement.name if replacement else None, left)
