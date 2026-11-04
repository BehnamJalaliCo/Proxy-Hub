"""The sponsor module: every live MTProto proxy registered in @MTProxybot by the operator account
(decision D37) with the wanted channel as Proxy Sponsor: @ipoxxo (D23) by default, a paying
customer's public channel during a campaign (docs/11 section 2, rows 1, 3, 4, 5 and 7).

What "registered" means (cross-team SPONSOR contract): a proxy has a registration once @MTProxybot
gave it a tag. Only then is its ``mtproxy_registrations`` row written. Before that the proxy is a
*candidate* (a ``sponsor_flows`` row alone), so team mtproto's ``proxies_to_register`` ("live
proxies without a registration") keeps listing it, with the secret the conversation needs, for as
many passes as it takes: a pass without a conversation, a failed try or «ثبت دوباره» never hides it.

One step of :func:`run_once` (the reconciler calls it every minute):

1. In short transactions: stop if the kill switch is ON (docs/11 lock 6); move campaigns that are
   due (scheduled -> active ``lead`` minutes before the start, -> finished at the end); ask the
   provider (team mtproto's ``sponsor_api``) for the live proxies without a registration. Each one
   is a candidate (new, or the one it already is, following its address), or, when a registration
   with the same ``host:port`` and user exists, gets that registration's tag back. A candidate
   whose node was deleted is dropped and a registration whose node was deleted is retired. A tag
   that did not reach its node is tried again every :data:`TAG_RETRY` (the provider never lists a
   registered proxy again). An owner alert that could not be queued before (no chat was paired) is
   queued now. Every ``apply_ad_tag`` runs in a transaction of its own (:func:`apply_tags`).
2. Pick at most ONE conversation: a campaign switch first, then a new registration, then setting
   the default sponsor. Nothing is picked while two or more are stuck (the bot is probably saying
   something new; the owner was alerted), while the hour's message budget cannot hold the whole
   conversation, or while the operator account is not connected.
3. Under the operator account's lock (``mtproxy_account``, shared with its login and session check;
   one flow at a time) connect, open the chat with @MTProxybot (only if it still resolves to the
   user id stored at login) and run the conversation (proxyhub/sponsor/driver.py). Every message is
   written to actions_log before it is sent, and every reply after it, with secrets masked.
4. Record the outcome in a short transaction: the tag (the registration row, then
   ``apply_ad_tag``), the sponsor channel, a retry time with backoff, or "stuck": the bot's last
   reply (masked) is kept for the dashboard and ONE Persian alert goes to the owner.

No database transaction is open while Telegram is called. No proxy secret is stored or logged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.db.models import SponsorCampaign
from proxyhub.fleet.actions import iso, log_action
from proxyhub.mtproxy import state as account_state
from proxyhub.mtproxy import store as account_store
from proxyhub.mtproxy.userclient import ClientOptions, MtpError, operator_session
from proxyhub.sponsor import campaigns as camp
from proxyhub.sponsor import driver, pacing, store
from proxyhub.sponsor.channels import ChannelError, channel_link, parse_public_channel
from proxyhub.sponsor.conversation import (
    MESSAGES_NEEDED,
    PromoteJob,
    PromotionSet,
    Registered,
    RegisterJob,
    Step,
    Stuck,
)
from proxyhub.sponsor.messages import HINT_FA, REASON_FA, STEP_FA, stuck_alert
from proxyhub.sponsor.provider import Registrable, SponsorProvider, node_of, user_of, valid
from proxyhub.sponsor.replies import short_tag
from proxyhub.sponsor.store import CLEAR_STUCK, FAILED, PENDING, REGISTERED, RETIRED, Reg
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

WORKER_ACTOR: Final = "sponsor"
PAUSE_AFTER_STUCK: Final = 2
MAX_SILENT: Final = 3
PROPAGATION: Final = timedelta(hours=1)  # روند @MTProxybot step 7
USE_STATES: Final = frozenset({"ready", "unhealthy", "repairing"})
GONE_STATES: Final = frozenset({"deleted"})
# Failures the account or the bot cannot get past by waiting: stop and tell the owner.
STUCK_ERRORS: Final = frozenset({"mtproxybot_changed", "mtproxybot_not_found", "telegram_refused"})
# Failures that are nobody's fault and do not count as an attempt.
NOT_COUNTED: Final = frozenset({"kill_switch", "hourly_cap", "hold", "pace_timeout"})
BACKOFF_MIN: Final = (5, 15, 30, 60, 120, 240, 360)
TAG_RETRY: Final = timedelta(minutes=10)


@dataclass(frozen=True)
class Deps:
    engine: AsyncEngine
    vault: Vault
    provider: SponsorProvider | None
    options: ClientOptions = field(default_factory=ClientOptions)
    timing: driver.Timing = field(default_factory=driver.Timing)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    lock_wait_s: float = 5.0


@dataclass(frozen=True)
class Work:
    reg: Reg
    job: RegisterJob | PromoteJob
    key: str | None
    priority: int

    def __repr__(self) -> str:
        return f"Work(reg={self.reg.id}, job={self.job!r}, priority={self.priority})"


async def _log(
    db: AsyncConnection,
    action: str,
    target: str | None,
    *,
    allowed: bool = True,
    reasons: Sequence[str] = (),
    context: dict[str, Any] | None = None,
    actor: str = WORKER_ACTOR,
) -> None:
    await log_action(
        db,
        actor=actor,
        action=action,
        target=target,
        dry_run=False,
        allowed=allowed,
        reasons=reasons,
        context=context or {},
    )


# --- what to do (pure) --------------------------------------------------------------------------------


def in_use(reg: Reg, nodes: dict[uuid.UUID, str]) -> bool:
    """A live node uses this registration now. A key without the contract's shape counts as in use
    (nothing is known against it); a registration that no proxy ever used does not."""
    if reg.proxy_key is None:
        return False
    node = node_of(reg.proxy_key)
    return True if node is None else nodes.get(node) in USE_STATES


def stuck_count(regs: Sequence[Reg]) -> int:
    return sum(1 for r in regs if r.stuck)


def choose(
    regs: Sequence[Reg],
    *,
    live: dict[str, Registrable],
    nodes: dict[uuid.UUID, str],
    wanted: str,
    default: str,
    campaign_channels: set[str],
    now: datetime,
) -> Work | None:
    """The one conversation to have now, or None. Priority: a switch to or from a campaign's
    channel (0), a new registration (1), the default sponsor for a registered proxy (2). A
    candidate is registered only while the provider lists it (its secret comes from there)."""
    if stuck_count(regs) >= PAUSE_AFTER_STUCK:
        return None
    found: list[tuple[int, datetime, str, Work]] = []
    for reg in regs:
        if not reg.due(now) or reg.stuck:
            continue
        if (
            not reg.candidate
            and reg.state == REGISTERED
            and reg.ad_tag is not None
            and reg.sponsor_channel != wanted
        ):
            if not in_use(reg, nodes):
                continue
            campaign = wanted != default or reg.sponsor_channel in campaign_channels
            priority = 0 if campaign else 2
            job: RegisterJob | PromoteJob = PromoteJob(host=reg.host, port=reg.port, channel=wanted)
        elif reg.candidate and reg.state == PENDING and reg.proxy_key in live:
            proxy = live[reg.proxy_key]
            if (proxy.host, proxy.port) != (reg.host, reg.port):
                continue
            priority, job = 1, RegisterJob(host=reg.host, port=reg.port, secret_hex=proxy.secret_hex)
        else:
            continue
        since = reg.registered_at or reg.created_at
        found.append(
            (priority, since, str(reg.id), Work(reg=reg, job=job, key=reg.proxy_key, priority=priority))
        )
    if not found:
        return None
    return min(found, key=lambda item: item[:3])[3]


def backoff(attempts: int, code: str, retry_after_s: int | None) -> timedelta:
    if code in NOT_COUNTED:
        return timedelta(minutes=10)
    delay = timedelta(minutes=BACKOFF_MIN[min(max(attempts, 1), len(BACKOFF_MIN)) - 1])
    if retry_after_s is not None and retry_after_s > 0:
        delay = max(delay, timedelta(seconds=retry_after_s + 5))
    return delay


# --- step 1: observe and match ------------------------------------------------------------------------


async def _advance_campaigns(db: AsyncConnection, settings: store.SponsorSettings, now: datetime) -> None:
    rows = await store.load_campaigns(db, for_update=True)
    for window, new_state in camp.transitions([c.window for c in rows], now, settings.lead):
        await store.set_campaign_state(db, window.id, new_state, now)
        await _log(
            db,
            f"sponsor.campaign_{new_state}",
            f"@{window.channel}",
            reasons=[f"{window.state}->{new_state}"],
            context={
                "campaign_id": str(window.id),
                "starts_at": iso(window.starts_at),
                "ends_at": iso(window.ends_at),
                "lead_minutes": settings.lead_minutes,
            },
        )


@dataclass(frozen=True)
class TagJob:
    """A registration's tag for the desired Telemt state of the proxy ``key`` (``apply_ad_tag``)."""

    flow_id: uuid.UUID
    key: str
    tag: str
    address: str
    announce: bool

    def __repr__(self) -> str:
        return f"TagJob(key={self.key!r}, tag={short_tag(self.tag)!r})"


async def apply_tags(
    engine: AsyncEngine, provider: SponsorProvider, jobs: Sequence[TagJob], now: datetime
) -> None:
    """Each tag in its OWN outermost transaction, after the module's own rows are committed (the
    tag is already safe in ``mtproxy_registrations``): the provider's write, the flow's bookkeeping
    and the log line commit together or not at all. Never inside a SAVEPOINT: team mtproto's
    ``apply_ad_tag`` writes the tag through an ORM session bound to the connection, which joins a
    savepoint with one of its own and rolls it back when it closes. A failure is logged and tried
    again after :data:`TAG_RETRY`."""
    for job in jobs:
        try:
            async with engine.begin() as db:
                await provider.apply_ad_tag(db, job.key, job.tag)
                await store.update_flow(db, job.flow_id, now, tag_applied_at=now, tag_failed_at=None)
                if job.announce:
                    await _log(
                        db,
                        "sponsor.tag_applied",
                        job.address,
                        reasons=["apply_ad_tag"],
                        context={"key": job.key, "tag": short_tag(job.tag)},
                    )
        except Exception as exc:
            log.warning("sponsor: apply_ad_tag failed (%s)", type(exc).__name__)
            async with engine.begin() as db:
                await store.update_flow(db, job.flow_id, now, tag_applied_at=None, tag_failed_at=now)
                await _log(
                    db,
                    "sponsor.tag_apply_failed",
                    job.address,
                    allowed=False,
                    reasons=[type(exc).__name__],
                    context={
                        "key": job.key,
                        "tag": short_tag(job.tag),
                        "next_try_at": iso(now + TAG_RETRY),
                    },
                )


def _tag_retry_due(reg: Reg, now: datetime) -> bool:
    return reg.tag_failed_at is None or reg.tag_failed_at <= now - TAG_RETRY


Identity = tuple[str, int, str]


def identity(host: str, port: int, secret_name: str) -> Identity:
    """A registration is ``host:port`` plus the Telemt user name (the unique key of
    ``mtproxy_registrations``). Team mtproto names users per node (``mt_<node>``), so a new node is
    a new registration; a provider that lists the same user at the same address again gets the
    existing registration and its tag back."""
    return (host, port, secret_name[:64])


def _best_by_identity(regs: Sequence[Reg]) -> dict[Identity, Reg]:
    """The registrations that hold a tag, the best one per identity (candidates never count)."""
    rank = {REGISTERED: 0, FAILED: 1, PENDING: 2, RETIRED: 3}
    best: dict[Identity, Reg] = {}
    for reg in regs:
        if reg.candidate or reg.ad_tag is None:
            continue
        key = identity(reg.host, reg.port, reg.secret_name)
        if key not in best or rank.get(reg.state, 9) < rank.get(best[key].state, 9):
            best[key] = reg
    return best


async def _match(
    db: AsyncConnection,
    proxy: Registrable,
    current: Reg | None,
    by_identity: dict[Identity, Reg],
    now: datetime,
) -> TagJob | None:
    """One listed proxy: follow it as a candidate, or give it back the tag of its registration
    (the returned job, applied after this transaction)."""
    address = f"{proxy.host}:{proxy.port}"
    known = by_identity.get(identity(proxy.host, proxy.port, user_of(proxy.key)))
    if current is not None and current.candidate:
        if known is None:
            if current.address != address:  # nothing is registered yet: the candidate follows it
                await store.update_flow(db, current.id, now, host=proxy.host, port=proxy.port)
                await _log(
                    db,
                    "sponsor.candidate_moved",
                    address,
                    reasons=["address_changed"],
                    context={"key": proxy.key, "was": current.address},
                )
            return None
        await store.delete_flow(db, current.id)
        await _log(
            db,
            "sponsor.candidate_dropped",
            current.address,
            reasons=["registration_exists"],
            context={"key": proxy.key, "registration": address},
        )
        current = None
    if current is not None and current.address != address:
        await store.update_reg(
            db, current.registration_id, now, state=RETIRED, state_reason="address_changed"
        )
        await store.update_flow(db, current.id, now, proxy_key=None, next_attempt_at=None, **CLEAR_STUCK)
        await _log(
            db,
            "sponsor.registration_retired",
            current.address,
            reasons=["address_changed"],
            context={"key": proxy.key, "now_at": address},
        )
    if known is None:
        await store.insert_candidate(
            db, host=proxy.host, port=proxy.port, user_name=user_of(proxy.key), proxy_key=proxy.key, now=now
        )
        await _log(
            db, "sponsor.proxy_found", address, reasons=["no_registration"], context={"key": proxy.key}
        )
        return None
    assert known.ad_tag is not None  # _best_by_identity keeps the registrations with a tag only
    revived = known.state == RETIRED
    moved = known.proxy_key != proxy.key
    if revived:
        await store.update_reg(db, known.registration_id, now, state=REGISTERED, state_reason=None)
        await _log(
            db,
            "sponsor.registration_reused",
            address,
            reasons=["same_address_and_user"],
            context={"key": proxy.key, "tag": short_tag(known.ad_tag)},
        )
    if moved:
        await store.update_flow(
            db, known.id, now, proxy_key=proxy.key, tag_applied_at=None, tag_failed_at=None
        )
    if revived or moved or _tag_retry_due(known, now):
        # Listed although it has a registration: the node does not carry the tag.
        return TagJob(
            flow_id=known.id, key=proxy.key, tag=known.ad_tag, address=address, announce=revived or moved
        )
    return None


async def sync(
    db: AsyncConnection, proxies: Sequence[Registrable], now: datetime
) -> tuple[list[Reg], dict[uuid.UUID, str], dict[str, Registrable], list[TagJob]]:
    """Match the provider's live proxies to candidates and registrations (module docstring,
    step 1). The tags to (re)apply come back as jobs for :func:`apply_tags`."""
    jobs: list[TagJob] = []
    live: dict[str, Registrable] = {}
    skipped = 0
    for proxy in proxies:
        if valid(proxy):
            live[proxy.key] = proxy
        else:
            skipped += 1
    if skipped:
        log.warning("sponsor: %d proxies from the provider do not have the contract's shape", skipped)
    await store.adopt_registrations(db, now)
    regs = await store.load_regs(db, for_update=True)
    by_identity = _best_by_identity(regs)
    by_key = {r.proxy_key: r for r in regs if r.state != RETIRED and r.proxy_key is not None}
    for proxy in live.values():
        if (job := await _match(db, proxy, by_key.get(proxy.key), by_identity, now)) is not None:
            jobs.append(job)
    regs = await store.load_regs(db)
    nodes = await store.node_states(db, {n for r in regs if (n := node_of(r.proxy_key)) is not None})
    for reg in regs:
        node = node_of(reg.proxy_key)
        if reg.state == RETIRED or node is None or reg.proxy_key in live:
            continue
        if nodes.get(node, "deleted") not in GONE_STATES:
            continue
        if reg.candidate:  # nothing was registered for it
            await store.delete_flow(db, reg.id)
            await _log(
                db,
                "sponsor.candidate_dropped",
                reg.address,
                reasons=["node_gone"],
                context={"key": reg.proxy_key},
            )
            continue
        await store.update_reg(db, reg.registration_id, now, state=RETIRED, state_reason="node_gone")
        await store.update_flow(db, reg.id, now, next_attempt_at=None, **CLEAR_STUCK)
        await _log(
            db,
            "sponsor.registration_retired",
            reg.address,
            reasons=["node_gone"],
            context={"key": reg.proxy_key},
        )
    for reg in await store.load_regs(db):
        if (
            not reg.candidate
            and reg.ad_tag is not None
            and reg.state != RETIRED
            and reg.proxy_key is not None
            and reg.proxy_key not in live
            and reg.tag_applied_at is None
            and node_of(reg.proxy_key) is not None
            and in_use(reg, nodes)
            and _tag_retry_due(reg, now)
        ):
            jobs.append(
                TagJob(flow_id=reg.id, key=reg.proxy_key, tag=reg.ad_tag, address=reg.address, announce=True)
            )
    return await store.load_regs(db), nodes, live, jobs


# --- the owner alert ----------------------------------------------------------------------------------


async def _alert(
    db: AsyncConnection,
    reg: Reg,
    *,
    step: str,
    reason: str,
    hint: str | None,
    bot_text: str | None,
    paused: bool,
    now: datetime,
) -> bool:
    """ONE Persian alert per stuck flow. ``alerted_at`` is set only when the alert was queued: with
    no owner chat paired yet it is queued on a later pass (:func:`_late_alerts`)."""
    alert = stuck_alert(
        address=reg.address, step=step, reason=reason, hint=hint, bot_text=bot_text, paused=paused
    )
    if await enqueue_owner_alert(db, alert) is None:
        return False
    await store.update_flow(db, reg.id, now, alerted_at=now)
    return True


async def _late_alerts(db: AsyncConnection, regs: Sequence[Reg], now: datetime) -> None:
    paused = stuck_count(regs) >= PAUSE_AFTER_STUCK
    for reg in regs:
        if reg.stuck and reg.alerted_at is None:
            await _alert(
                db,
                reg,
                step=reg.last_step or "start",
                reason=reg.stuck_reason or "unknown",
                hint=reg.stuck_hint,
                bot_text=reg.last_bot_text,
                paused=paused,
                now=now,
            )


# --- step 3: the conversation -------------------------------------------------------------------------


class _Recorder:
    """Every message and reply of a conversation: one actions_log line each, in its own short
    transaction (secrets masked by the driver before they get here). A message is written BEFORE
    it goes out, so one that Telegram received while the call failed is on record too (lock 5)."""

    def __init__(self, engine: AsyncEngine, target: str) -> None:
        self._engine = engine
        self._target = target

    async def _line(self, action: str, step: Step, context: dict[str, Any]) -> None:
        async with self._engine.begin() as db:
            await _log(
                db, action, self._target, reasons=[step.value], context={"step": step.value, **context}
            )

    async def sending(self, step: Step, shown: str) -> None:
        await self._line("sponsor.bot_sent", step, {"text": shown})

    async def pressing(self, step: Step, label: str) -> None:
        await self._line("sponsor.bot_pressed", step, {"button": label})

    async def reply(self, step: Step, text: str, buttons: list[str]) -> None:
        await self._line("sponsor.bot_reply", step, {"text": text, "buttons": buttons})


async def _converse(deps: Deps, work: Work) -> driver.FlowResult | str:
    try:
        async with account_store.account_lock(deps.engine, wait_s=deps.lock_wait_s):
            async with deps.engine.connect() as db:
                if await account_store.kill_switch_on(db):
                    return "kill_switch"
                row = await account_store.load_row(db)
                if row.state != account_state.CONNECTED or row.mtproxybot_id is None:
                    return "no_account"
                creds = await account_store.load_creds(db, deps.vault)
                session = await account_store.load_session(db, deps.vault)
            if creds is None or session is None:
                return "no_account"

            async def take_slot() -> datetime:
                return await pacing.take_slot(deps.engine, clock=deps.clock, sleep=deps.sleep)

            recorder = _Recorder(deps.engine, work.job.address)
            try:
                async with operator_session(creds, session, options=deps.options) as op:
                    chat = await op.open_mtproxybot_chat(row.mtproxybot_id)
                    return await driver.run(
                        chat,
                        work.job,
                        take_slot=take_slot,
                        record=recorder,
                        timing=deps.timing,
                        sleep=deps.sleep,
                    )
            except MtpError as exc:
                return driver.FlowResult(error=exc.code, rpc=exc.rpc, retry_after_s=exc.seconds)
    except account_store.LockBusy:
        return "busy"
    except VaultError:
        return "vault_unavailable"


# --- step 4: record -----------------------------------------------------------------------------------


async def _stuck(
    db: AsyncConnection,
    reg: Reg,
    *,
    step: str,
    reason: str,
    hint: str | None,
    bot_text: str | None,
    rpc: str | None,
    now: datetime,
) -> str:
    await store.update_reg(db, reg.registration_id, now, state=FAILED, state_reason=reason)
    await store.update_flow(
        db,
        reg.id,
        now,
        attempts=reg.attempts + 1,
        next_attempt_at=None,
        last_error=reason,
        last_step=step,
        stuck_reason=reason,
        stuck_hint=hint,
        stuck_at=now,
        last_bot_text=bot_text,
    )
    stuck_now = stuck_count(await store.load_regs(db))
    if reg.alerted_at is None:
        await _alert(
            db,
            reg,
            step=step,
            reason=reason,
            hint=hint,
            bot_text=bot_text,
            paused=stuck_now >= PAUSE_AFTER_STUCK,
            now=now,
        )
    context: dict[str, Any] = {"step": step, "stuck_registrations": stuck_now}
    if rpc:
        context["telegram_error"] = rpc
    await _log(
        db,
        "sponsor.flow_stuck",
        reg.address,
        allowed=False,
        reasons=[reason, *([hint] if hint else [])],
        context=context,
    )
    return f"stuck:{reason}"


async def _registered(
    db: AsyncConnection, work: Work, reg: Reg | None, tag: str, step: str, now: datetime
) -> tuple[str, TagJob | None]:
    """@MTProxybot gave a tag: the registration row is written now, and the tag goes to the node
    (the returned job, applied once this transaction committed). If the proxy's row went away
    meanwhile (its node was deleted), the entry still exists in @MTProxybot: it stays on record,
    retired, with its tag, and no node gets it."""
    gone = reg is None
    base = work.reg if reg is None else reg
    if reg is None:
        flow_id = await store.insert_candidate(
            db, host=base.host, port=base.port, user_name=base.secret_name, proxy_key=None, now=now
        )
    else:
        flow_id = reg.id
    await store.create_registration(
        db,
        flow_id,
        host=base.host,
        port=base.port,
        secret_name=base.secret_name,
        ad_tag=tag,
        now=now,
        state=RETIRED if gone else REGISTERED,
        state_reason="proxy_gone" if gone else None,
    )
    await store.update_flow(
        db,
        flow_id,
        now,
        attempts=0,
        next_attempt_at=None,
        last_error=None,
        last_step=step,
        tag_applied_at=None,
        tag_failed_at=None,
        **CLEAR_STUCK,
    )
    await _log(
        db,
        "sponsor.registered",
        base.address,
        reasons=["tag_received", *(["proxy_gone"] if gone else [])],
        context={"tag": short_tag(tag), "key": work.key},
    )
    if gone or work.key is None:
        return "registered", None
    return "registered", TagJob(flow_id=flow_id, key=work.key, tag=tag, address=base.address, announce=True)


async def _record(deps: Deps, work: Work, outcome: driver.FlowResult) -> str:
    now = deps.clock()
    async with deps.engine.begin() as db:
        done, job = await _settle(db, work, outcome, now)
    if job is not None and deps.provider is not None:
        await apply_tags(deps.engine, deps.provider, [job], now)
    if done.startswith("failed:"):
        log.warning("sponsor: conversation with @MTProxybot failed (%s)", done.removeprefix("failed:"))
    return done


async def _settle(
    db: AsyncConnection, work: Work, outcome: driver.FlowResult, now: datetime
) -> tuple[str, TagJob | None]:
    """What the conversation ended with, written in the caller's transaction."""
    reg = await store.load_reg(db, work.reg.id, for_update=True)
    step = outcome.last_step.value if outcome.last_step is not None else "start"
    result = outcome.result
    if isinstance(result, Registered):
        return await _registered(db, work, reg, result.tag, step, now)
    if reg is None:
        return "gone", None
    return await _settle_reg(db, reg, outcome, step, now), None


async def _settle_reg(
    db: AsyncConnection, reg: Reg, outcome: driver.FlowResult, step: str, now: datetime
) -> str:
    result = outcome.result
    if isinstance(result, PromotionSet):
        await store.update_reg(db, reg.registration_id, now, sponsor_channel=result.channel)
        await store.update_flow(
            db,
            reg.id,
            now,
            attempts=0,
            next_attempt_at=None,
            last_error=None,
            last_step=step,
            promotion_set_at=now,
            **CLEAR_STUCK,
        )
        await _log(
            db,
            "sponsor.promotion_set",
            reg.address,
            reasons=["set_promotion"],
            context={"channel": f"@{result.channel}"},
        )
        return "promotion_set"
    if isinstance(result, Stuck):
        return await _stuck(
            db,
            reg,
            step=result.step.value,
            reason=result.reason,
            hint=result.hint,
            bot_text=outcome.last_bot_text,
            rpc=None,
            now=now,
        )
    code = outcome.error or "unknown"
    if code in STUCK_ERRORS or (
        code == "no_reply" and reg.last_error == "no_reply" and reg.attempts + 1 >= MAX_SILENT
    ):
        return await _stuck(
            db, reg, step=step, reason=code, hint=None, bot_text=None, rpc=outcome.rpc, now=now
        )
    attempts = reg.attempts + (0 if code in NOT_COUNTED else 1)
    retry_at = now + backoff(attempts, code, outcome.retry_after_s)
    if code == "flood_wait" and outcome.retry_after_s:
        # Telegram asked the ACCOUNT to wait: no message to anyone until then (lock 4).
        await pacing.hold(db, now + timedelta(seconds=outcome.retry_after_s + 5), now)
    await store.update_flow(
        db, reg.id, now, attempts=attempts, next_attempt_at=retry_at, last_error=code, last_step=step
    )
    context: dict[str, Any] = {
        "step": step,
        "next_attempt_at": iso(retry_at),
        "messages_sent": outcome.messages_sent,
    }
    if outcome.rpc:
        context["telegram_error"] = outcome.rpc
    if outcome.retry_after_s is not None:
        context["retry_after_s"] = outcome.retry_after_s
    await _log(db, "sponsor.flow_failed", reg.address, allowed=False, reasons=[code], context=context)
    return f"failed:{code}"


# --- one step -----------------------------------------------------------------------------------------


async def run_once(deps: Deps) -> str:
    """One pass (module docstring). Returns what happened: ``kill_switch``, ``no_provider``,
    ``no_account``, ``paused``, ``idle``, ``paced``, ``busy``, ``vault_unavailable``, ``registered``,
    ``promotion_set``, ``stuck:<reason>``, ``failed:<code>``, ``gone``."""
    now = deps.clock()
    async with deps.engine.begin() as db:
        if await account_store.kill_switch_on(db):
            return "kill_switch"
        settings = await store.load_settings(db)
        await _advance_campaigns(db, settings, now)
    provider = deps.provider
    if provider is None:
        return "no_provider"
    proxies: list[Registrable] = []
    try:
        # A transaction of its own: a provider failure undoes nothing here, and the provider never
        # runs inside a SAVEPOINT (see apply_tags).
        async with deps.engine.begin() as db:
            proxies = list(await provider.proxies_to_register(db))
    except Exception as exc:
        log.warning("sponsor: the provider failed to list proxies (%s)", type(exc).__name__)
    async with deps.engine.begin() as db:
        regs, nodes, live, tag_jobs = await sync(db, proxies, now)
        await _late_alerts(db, regs, now)
        windows = [c.window for c in await store.load_campaigns(db)]
        account = await account_store.load_row(db)
        pace = await pacing.load(db)
    await apply_tags(deps.engine, provider, tag_jobs, now)
    if account.state != account_state.CONNECTED or account.mtproxybot_id is None:
        return "no_account"
    if stuck_count(regs) >= PAUSE_AFTER_STUCK:
        return "paused"
    wanted = camp.wanted_channel(windows, settings.default_channel, now, settings.lead)
    work = choose(
        regs,
        live=live,
        nodes=nodes,
        wanted=wanted,
        default=settings.default_channel,
        campaign_channels={w.channel for w in windows},
        now=now,
    )
    if work is None:
        return "idle"
    if pacing.remaining(pace, now) < MESSAGES_NEEDED[type(work.job)]:
        return "paced"
    outcome = await _converse(deps, work)
    if isinstance(outcome, str):
        return outcome
    return await _record(deps, work, outcome)


# --- the owner's view and actions (API) ----------------------------------------------------------------


def _reg_view(reg: Reg, *, wanted: str, nodes: dict[uuid.UUID, str], now: datetime) -> dict[str, Any]:
    node = node_of(reg.proxy_key)
    stuck = None
    if reg.stuck:
        stuck = {
            "reason": reg.stuck_reason,
            "reason_fa": REASON_FA.get(reg.stuck_reason or "", reg.stuck_reason),
            "hint": reg.stuck_hint,
            "hint_fa": HINT_FA.get(reg.stuck_hint or ""),
            "step": reg.last_step,
            "step_fa": STEP_FA.get(reg.last_step or ""),
            "at": iso(reg.stuck_at),
            "bot_text": reg.last_bot_text,
        }
    propagated_at = reg.promotion_set_at + PROPAGATION if reg.promotion_set_at is not None else None
    return {
        "id": str(reg.id),
        "address": reg.address,
        "host": reg.host,
        "port": reg.port,
        "node_id": str(node) if node is not None else None,
        "node_state": nodes.get(node) if node is not None else None,
        "in_use": reg.state != RETIRED and in_use(reg, nodes),
        "state": reg.state,
        "state_reason": reg.state_reason,
        "has_tag": reg.ad_tag is not None,
        "tag_hint": short_tag(reg.ad_tag),
        # The tag is in the node's desired Telemt state (None: no tag, or no node uses it).
        "tag_on_node": (reg.tag_applied_at is not None)
        if reg.ad_tag is not None and reg.state != RETIRED and reg.proxy_key is not None
        else None,
        "sponsor_channel": reg.sponsor_channel,
        "wanted_channel": wanted if reg.state != RETIRED else None,
        "in_sync": reg.ad_tag is not None and reg.sponsor_channel == wanted,
        "registered_at": iso(reg.registered_at),
        "promotion_set_at": iso(reg.promotion_set_at),
        "visible_from": iso(propagated_at) if propagated_at is not None and propagated_at > now else None,
        "attempts": reg.attempts,
        "next_attempt_at": iso(reg.next_attempt_at),
        "last_error": reg.last_error,
        "stuck": stuck,
    }


def _campaign_view(c: store.Campaign, *, lead: timedelta, now: datetime) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "customer": c.customer,
        "channel": c.channel,
        "link": channel_link(c.channel),
        "starts_at": iso(c.starts_at),
        "ends_at": iso(c.ends_at),
        "switch_at": iso(c.window.switch_at(lead)),
        "price": str(c.price) if c.price is not None else None,
        "currency": c.currency,
        "paid": c.paid,
        "state": c.state,
        "effective": c.window.effective(now, lead),
        "created_at": iso(c.created_at),
    }


async def overview(engine: AsyncEngine, *, now: datetime, provider_ready: bool) -> dict[str, Any]:
    async with engine.connect() as db:
        kill_switch = await account_store.kill_switch_on(db)
        settings = await store.load_settings(db)
        regs = await store.load_regs(db)
        campaigns = await store.load_campaigns(db)
        account = await account_store.load_row(db)
        pace = await pacing.load(db)
        nodes = await store.node_states(db, {n for r in regs if (n := node_of(r.proxy_key)) is not None})
    windows = [c.window for c in campaigns]
    effective = camp.effective_campaign(windows, now, settings.lead)
    wanted = effective.channel if effective is not None else settings.default_channel
    stuck_now = stuck_count(regs)
    return {
        "settings": settings.to_json(),
        "kill_switch": kill_switch,
        "provider_ready": provider_ready,
        "account": {
            "state": account.state,
            "connected": account.state == account_state.CONNECTED,
            "mtproxybot_ok": account.mtproxybot_id is not None,
        },
        "paused": stuck_now >= PAUSE_AFTER_STUCK,
        "stuck_count": stuck_now,
        "pause_after_stuck": PAUSE_AFTER_STUCK,
        "pacing": {
            "sent_last_hour": len(pace.recent(now)),
            "hourly_cap": pacing.HOURLY_CAP,
            "min_gap_s": int(pacing.MIN_GAP.total_seconds()),
            "hold_until": iso(pace.hold_until) if pace.held(now) else None,
        },
        "wanted_channel": wanted,
        "active_campaign_id": str(effective.id) if effective is not None else None,
        "registrations": [
            _reg_view(r, wanted=wanted, nodes=nodes, now=now)
            for r in sorted(regs, key=lambda r: (r.state == RETIRED, r.host, r.port))
        ],
        "campaigns": [_campaign_view(c, lead=settings.lead, now=now) for c in campaigns],
    }


class SponsorError(Exception):
    """An API error code (proxyhub/sponsor/messages.py)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def retry(engine: AsyncEngine, reg_id: uuid.UUID, *, actor: str, now: datetime) -> None:
    """«ثبت دوباره»: a stuck proxy goes back to work. One without a tag is registered again: it has
    no registration row, so the provider keeps listing it with its secret. One with a tag (stuck or
    not) has its sponsor set again. The alert may fire again if it gets stuck again."""
    async with engine.begin() as db:
        reg = await store.load_reg(db, reg_id, for_update=True)
        if reg is None:
            raise SponsorError("not_found")
        if reg.state == RETIRED:
            raise SponsorError("registration_retired")
        if reg.ad_tag is not None:
            await store.update_reg(
                db, reg.registration_id, now, state=REGISTERED, state_reason=None, sponsor_channel=None
            )
        # A tag that did not reach its node is tried again on the next pass, without the pause.
        await store.update_flow(
            db,
            reg.id,
            now,
            attempts=0,
            next_attempt_at=None,
            last_error=None,
            tag_failed_at=None,
            **CLEAR_STUCK,
        )
        await _log(
            db,
            "sponsor.retry_requested",
            reg.address,
            reasons=[f"from:{reg.state}"],
            context={"stuck_reason": reg.stuck_reason, "has_tag": reg.ad_tag is not None},
            actor=actor,
        )


async def save_settings(engine: AsyncEngine, *, default_channel: str, lead_minutes: int, actor: str) -> None:
    try:
        channel = parse_public_channel(default_channel)
    except ChannelError as exc:
        raise SponsorError(exc.code) from None
    if not store.MIN_LEAD_MIN <= lead_minutes <= store.MAX_LEAD_MIN:
        raise SponsorError("lead_format")
    new = store.SponsorSettings(default_channel=channel, lead_minutes=lead_minutes)
    async with engine.begin() as db:
        old = await store.load_settings(db)
        await store.save_settings(db, new)
        await _log(
            db,
            "sponsor.settings_changed",
            "sponsor",
            reasons=["owner"],
            context={"before": old.to_json(), "after": new.to_json()},
            actor=actor,
        )


@dataclass(frozen=True)
class CampaignInput:
    customer: str | None
    channel: str
    starts_at: datetime
    ends_at: datetime
    price: Decimal | None
    currency: str | None
    paid: bool


def _clean_campaign(data: CampaignInput, default_channel: str) -> tuple[str, str]:
    try:
        channel = parse_public_channel(data.channel)
    except ChannelError as exc:
        raise SponsorError(exc.code) from None
    if channel == default_channel:
        raise SponsorError("channel_is_default")
    customer = (data.customer or "").strip() or f"@{channel}"
    if len(customer) > 200:
        raise SponsorError("customer_format")
    return channel, customer


async def create_campaign(
    engine: AsyncEngine, data: CampaignInput, *, actor: str, now: datetime
) -> uuid.UUID:
    async with engine.begin() as db:
        await store.lock_campaigns(db)
        settings = await store.load_settings(db)
        channel, customer = _clean_campaign(data, settings.default_channel)
        others = [c.window for c in await store.load_campaigns(db)]
        try:
            camp.check_window(data.starts_at, data.ends_at, now=now, others=others)
        except camp.CampaignError as exc:
            raise SponsorError(exc.code) from None
        campaign_id = uuid.uuid4()
        await db.execute(
            sa_insert(SponsorCampaign).values(
                id=campaign_id,
                customer=customer,
                channel=channel,
                starts_at=data.starts_at,
                ends_at=data.ends_at,
                price=data.price,
                currency=data.currency,
                paid=data.paid,
                registration_ids={},
                state=camp.SCHEDULED,
                created_at=now,
                updated_at=now,
            )
        )
        await _log(
            db,
            "sponsor.campaign_created",
            f"@{channel}",
            reasons=["owner"],
            context={
                "campaign_id": str(campaign_id),
                "starts_at": iso(data.starts_at),
                "ends_at": iso(data.ends_at),
            },
            actor=actor,
        )
    return campaign_id


async def update_campaign(
    engine: AsyncEngine, campaign_id: uuid.UUID, data: CampaignInput, *, actor: str, now: datetime
) -> None:
    async with engine.begin() as db:
        await store.lock_campaigns(db)
        current = await store.load_campaign(db, campaign_id, for_update=True)
        if current is None:
            raise SponsorError("not_found")
        if current.state not in camp.OPEN_STATES:
            raise SponsorError("campaign_closed")
        settings = await store.load_settings(db)
        channel, customer = _clean_campaign(data, settings.default_channel)
        started = current.state == camp.ACTIVE
        starts_at = current.starts_at if started else data.starts_at
        others = [c.window for c in await store.load_campaigns(db)]
        try:
            camp.check_window(
                starts_at,
                data.ends_at,
                now=now,
                others=others,
                editing=campaign_id,
                already_started=started,
            )
        except camp.CampaignError as exc:
            raise SponsorError(exc.code) from None
        await db.execute(
            sa_update(SponsorCampaign)
            .where(SponsorCampaign.id == campaign_id)
            .values(
                customer=customer,
                channel=channel,
                starts_at=starts_at,
                ends_at=data.ends_at,
                price=data.price,
                currency=data.currency,
                paid=data.paid,
                updated_at=now,
            )
        )
        await _log(
            db,
            "sponsor.campaign_updated",
            f"@{channel}",
            reasons=["owner"],
            context={
                "campaign_id": str(campaign_id),
                "starts_at": iso(starts_at),
                "ends_at": iso(data.ends_at),
            },
            actor=actor,
        )


async def cancel_campaign(engine: AsyncEngine, campaign_id: uuid.UUID, *, actor: str, now: datetime) -> None:
    """The sponsor goes back to the default channel on the next pass (if the campaign was on)."""
    async with engine.begin() as db:
        await store.lock_campaigns(db)
        current = await store.load_campaign(db, campaign_id, for_update=True)
        if current is None:
            raise SponsorError("not_found")
        if current.state not in camp.OPEN_STATES:
            raise SponsorError("campaign_closed")
        await store.set_campaign_state(db, campaign_id, camp.CANCELLED, now)
        await _log(
            db,
            "sponsor.campaign_cancelled",
            f"@{current.channel}",
            reasons=[f"from:{current.state}"],
            context={"campaign_id": str(campaign_id)},
            actor=actor,
        )


async def delete_campaign(engine: AsyncEngine, campaign_id: uuid.UUID, *, actor: str, now: datetime) -> None:
    """Only a campaign that never started: one that ran stays on record (revenue, reports)."""
    async with engine.begin() as db:
        await store.lock_campaigns(db)
        current = await store.load_campaign(db, campaign_id, for_update=True)
        if current is None:
            raise SponsorError("not_found")
        if current.state != camp.SCHEDULED or now >= current.window.switch_at(
            (await store.load_settings(db)).lead
        ):
            raise SponsorError("campaign_started")
        await db.execute(sa_delete(SponsorCampaign).where(SponsorCampaign.id == campaign_id))
        await _log(
            db,
            "sponsor.campaign_deleted",
            f"@{current.channel}",
            reasons=["never_started"],
            context={"campaign_id": str(campaign_id)},
            actor=actor,
        )
