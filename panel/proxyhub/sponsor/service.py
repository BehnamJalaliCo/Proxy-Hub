"""The sponsor module: every live MTProto proxy registered in @MTProxybot by the operator account
(decision D37) with the wanted channel as Proxy Sponsor: @ipoxxo (D23) by default, a paying
customer's public channel during a campaign (docs/11 section 2, rows 1, 3, 4, 5 and 7).

One step of :func:`run_once` (the reconciler calls it every minute):

1. In ONE short transaction: stop if the kill switch is ON (docs/11 lock 6); move campaigns that are
   due (scheduled -> active ``lead`` minutes before the start, -> finished at the end); ask the
   provider (team mtproto's ``sponsor_api``) for the live proxies without a registration and match
   each to a registration by ``host:port`` and user name (a new row, or the existing one whose tag
   is put on the node again with ``apply_ad_tag``); retire registrations whose node was deleted.
2. Pick at most ONE conversation: a campaign switch first, then a new registration, then setting
   the default sponsor. Nothing is picked while two or more registrations are stuck (the bot is
   probably saying something new; the owner was alerted), while the hour's message budget cannot
   hold the whole conversation, or while the operator account is not connected.
3. Under the operator account's lock (``mtproxy_account``, shared with its login and session check;
   one flow at a time) connect, open the chat with @MTProxybot (only if it still resolves to the
   user id stored at login) and run the conversation (proxyhub/sponsor/driver.py). Every message
   and reply goes to actions_log with secrets masked.
4. Record the outcome in a short transaction: the tag (and ``apply_ad_tag``), the sponsor channel,
   a retry time with backoff, or "stuck": the bot's last reply (masked) is kept for the dashboard
   and ONE Persian alert goes to the owner.

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
    return sum(1 for r in regs if r.state == FAILED)


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
    channel (0), a new registration (1), the default sponsor for a registered proxy (2)."""
    if stuck_count(regs) >= PAUSE_AFTER_STUCK:
        return None
    found: list[tuple[int, datetime, str, Work]] = []
    for reg in regs:
        if not reg.due(now):
            continue
        if reg.state == REGISTERED and reg.ad_tag is not None and reg.sponsor_channel != wanted:
            if not in_use(reg, nodes):
                continue
            campaign = wanted != default or reg.sponsor_channel in campaign_channels
            priority = 0 if campaign else 2
            job: RegisterJob | PromoteJob = PromoteJob(host=reg.host, port=reg.port, channel=wanted)
        elif reg.state == PENDING and reg.ad_tag is None and reg.proxy_key in live:
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


async def _apply_tag(
    db: AsyncConnection, provider: SponsorProvider, key: str, tag: str, address: str, *, announce: bool
) -> None:
    """Put the tag into the node's desired Telemt state (the provider's own transaction rules
    apply; a failure is logged and retried on the next pass, and never loses the tag)."""
    try:
        async with db.begin_nested():
            await provider.apply_ad_tag(db, key, tag)
    except Exception as exc:
        log.warning("sponsor: apply_ad_tag failed (%s)", type(exc).__name__)
        await _log(
            db,
            "sponsor.tag_apply_failed",
            address,
            allowed=False,
            reasons=[type(exc).__name__],
            context={"key": key, "tag": short_tag(tag)},
        )
        return
    if announce:
        await _log(
            db,
            "sponsor.tag_applied",
            address,
            reasons=["apply_ad_tag"],
            context={"key": key, "tag": short_tag(tag)},
        )


Identity = tuple[str, int, str]


def identity(host: str, port: int, secret_name: str) -> Identity:
    """A registration is ``host:port`` plus the Telemt user name (the unique key of
    ``mtproxy_registrations``): a replacement node answering at the same slot name and port with
    the same user name reuses the registration and its tag."""
    return (host, port, secret_name[:64])


def _best_by_identity(regs: Sequence[Reg]) -> dict[Identity, Reg]:
    rank = {REGISTERED: 0, FAILED: 1, PENDING: 2, RETIRED: 3}
    best: dict[Identity, Reg] = {}
    for reg in regs:
        key = identity(reg.host, reg.port, reg.secret_name)
        if key not in best or rank.get(reg.state, 9) < rank.get(best[key].state, 9):
            best[key] = reg
    return best


async def sync(
    db: AsyncConnection, provider: SponsorProvider, proxies: Sequence[Registrable], now: datetime
) -> tuple[list[Reg], dict[uuid.UUID, str], dict[str, Registrable]]:
    """Match the provider's live proxies to registrations (see the module docstring, step 1)."""
    live: dict[str, Registrable] = {}
    skipped = 0
    for proxy in proxies:
        if valid(proxy):
            live[proxy.key] = proxy
        else:
            skipped += 1
    if skipped:
        log.warning("sponsor: %d proxies from the provider do not have the contract's shape", skipped)
    regs = await store.load_regs(db, for_update=True)
    by_identity = _best_by_identity(regs)
    by_key = {r.proxy_key: r for r in regs if r.state != RETIRED and r.proxy_key is not None}
    for proxy in live.values():
        address = f"{proxy.host}:{proxy.port}"
        moved = by_key.get(proxy.key)
        if moved is not None and (moved.host, moved.port) != (proxy.host, proxy.port):
            await store.update_reg(db, moved.id, now, state=RETIRED, state_reason="address_changed")
            await store.update_flow(db, moved.id, now, proxy_key=None, next_attempt_at=None, **CLEAR_STUCK)
            await _log(
                db,
                "sponsor.registration_retired",
                moved.address,
                reasons=["address_changed"],
                context={"key": proxy.key, "now_at": address},
            )
        wanted_id = identity(proxy.host, proxy.port, user_of(proxy.key))
        reg = by_identity.get(wanted_id)
        if reg is None:
            reg_id = await store.insert_reg(
                db,
                host=proxy.host,
                port=proxy.port,
                secret_name=user_of(proxy.key),
                proxy_key=proxy.key,
                now=now,
            )
            by_identity[wanted_id] = Reg(**_blank(reg_id, proxy, now))
            await _log(
                db, "sponsor.proxy_found", address, reasons=["no_registration"], context={"key": proxy.key}
            )
            continue
        revived = reg.state == RETIRED
        if revived:
            await store.update_reg(
                db, reg.id, now, state=REGISTERED if reg.ad_tag else PENDING, state_reason=None
            )
            await _log(
                db,
                "sponsor.registration_reused",
                address,
                reasons=["same_address_and_user"],
                context={"key": proxy.key, "tag": short_tag(reg.ad_tag)},
            )
        if reg.proxy_key != proxy.key:
            await store.update_flow(db, reg.id, now, proxy_key=proxy.key)
        if reg.ad_tag is not None:
            await _apply_tag(
                db, provider, proxy.key, reg.ad_tag, address, announce=revived or reg.proxy_key != proxy.key
            )
    regs = await store.load_regs(db)
    nodes = await store.node_states(db, {n for r in regs if (n := node_of(r.proxy_key)) is not None})
    for reg in regs:
        node = node_of(reg.proxy_key)
        if reg.state == RETIRED or node is None or reg.proxy_key in live:
            continue
        if nodes.get(node, "deleted") in GONE_STATES:
            await store.update_reg(db, reg.id, now, state=RETIRED, state_reason="node_gone")
            await store.update_flow(db, reg.id, now, next_attempt_at=None, **CLEAR_STUCK)
            await _log(
                db,
                "sponsor.registration_retired",
                reg.address,
                reasons=["node_gone"],
                context={"key": reg.proxy_key},
            )
    return await store.load_regs(db), nodes, live


def _blank(reg_id: uuid.UUID, proxy: Registrable, now: datetime) -> dict[str, Any]:
    return {
        "id": reg_id,
        "host": proxy.host,
        "port": proxy.port,
        "secret_name": user_of(proxy.key),
        "ad_tag": None,
        "sponsor_channel": None,
        "state": PENDING,
        "state_reason": None,
        "registered_at": None,
        "created_at": now,
        "proxy_key": proxy.key,
        "attempts": 0,
        "next_attempt_at": None,
        "last_error": None,
        "last_step": None,
        "stuck_reason": None,
        "stuck_hint": None,
        "stuck_at": None,
        "last_bot_text": None,
        "alerted_at": None,
        "promotion_set_at": None,
    }


# --- step 3: the conversation -------------------------------------------------------------------------


class _Recorder:
    """Every message and reply of a conversation: one actions_log line each (secrets masked by
    the driver before they get here)."""

    def __init__(self, engine: AsyncEngine, target: str) -> None:
        self._engine = engine
        self._target = target

    async def _line(self, action: str, step: Step, context: dict[str, Any]) -> None:
        async with self._engine.begin() as db:
            await _log(
                db, action, self._target, reasons=[step.value], context={"step": step.value, **context}
            )

    async def sent(self, step: Step, shown: str) -> None:
        await self._line("sponsor.bot_sent", step, {"text": shown})

    async def pressed(self, step: Step, label: str, answer: str | None) -> None:
        await self._line("sponsor.bot_pressed", step, {"button": label, "answer": answer})

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
    await store.update_reg(db, reg.id, now, state=FAILED, state_reason=reason)
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
        alert = stuck_alert(
            address=reg.address,
            step=step,
            reason=reason,
            hint=hint,
            bot_text=bot_text,
            paused=stuck_now >= PAUSE_AFTER_STUCK,
        )
        await enqueue_owner_alert(db, alert)
        await store.update_flow(db, reg.id, now, alerted_at=now)
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


async def _record(deps: Deps, work: Work, outcome: driver.FlowResult) -> str:
    now = deps.clock()
    async with deps.engine.begin() as db:
        reg = await store.load_reg(db, work.reg.id, for_update=True)
        if reg is None:
            return "gone"
        step = outcome.last_step.value if outcome.last_step is not None else "start"
        result = outcome.result
        if isinstance(result, Registered):
            # Never lose a tag the bot gave, even if the row was retired meanwhile.
            await store.update_reg(
                db,
                reg.id,
                now,
                ad_tag=result.tag,
                registered_at=now,
                **({} if reg.state == RETIRED else {"state": REGISTERED, "state_reason": None}),
            )
            await store.update_flow(
                db,
                reg.id,
                now,
                attempts=0,
                next_attempt_at=None,
                last_error=None,
                last_step=step,
                **CLEAR_STUCK,
            )
            await _log(
                db,
                "sponsor.registered",
                reg.address,
                reasons=["tag_received"],
                context={"tag": short_tag(result.tag), "key": work.key},
            )
            if work.key is not None and deps.provider is not None and reg.state != RETIRED:
                await _apply_tag(db, deps.provider, work.key, result.tag, reg.address, announce=True)
            return "registered"
        if isinstance(result, PromotionSet):
            await store.update_reg(db, reg.id, now, sponsor_channel=result.channel)
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
    log.warning("sponsor: conversation with @MTProxybot failed (%s)", code)
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
        if deps.provider is None:
            return "no_provider"
        proxies = list(await deps.provider.proxies_to_register(db))
        regs, nodes, live = await sync(db, deps.provider, proxies, now)
        windows = [c.window for c in await store.load_campaigns(db)]
        account = await account_store.load_row(db)
        pace = await pacing.load(db)
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
    if reg.state == FAILED:
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
    """«ثبت دوباره»: a stuck registration goes back to work (registering again if it has no tag,
    else setting its sponsor again); a registered one has its sponsor set again; the alert may fire
    again if it gets stuck again."""
    async with engine.begin() as db:
        reg = await store.load_reg(db, reg_id, for_update=True)
        if reg is None:
            raise SponsorError("not_found")
        if reg.state == RETIRED:
            raise SponsorError("registration_retired")
        values: dict[str, Any] = {"state_reason": None}
        if reg.ad_tag is None:
            values["state"] = PENDING
        else:
            values.update(state=REGISTERED, sponsor_channel=None)
        await store.update_reg(db, reg.id, now, **values)
        await store.update_flow(
            db, reg.id, now, attempts=0, next_attempt_at=None, last_error=None, **CLEAR_STUCK
        )
        await _log(
            db,
            "sponsor.retry_requested",
            reg.address,
            reasons=[f"from:{reg.state}"],
            context={"stuck_reason": reg.stuck_reason},
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
