"""@MTProxybot's own list of our proxies: the inventory, the promotion check and the cleanup of the
proxies whose node was deleted (docs/11 section 2, decisions D25 and D37). The walks are pure state
machines in proxyhub/sponsor/botlist.py; this module keeps the job, the entries and the owner's view.

One job at a time (settings row :data:`KEY`), started by the owner's button or the CLI
(``python -m proxyhub.sponsor``), or **by the panel itself** (P30, 2026-11-07: the owner should not
have to press anything when a node goes): with removal on, a ``cleanup`` is queued when a
registration was retired as ``node_gone`` at least :data:`AUTO_AFTER` ago and after the last
cleanup was queued (:func:`auto_cleanup_due`; one whose cleanup did not finish ``done`` is offered
again :data:`CHECK_RETRY` later), at most one job per :data:`AUTO_GAP`, never after a job that got
stuck (the owner looks at those first, and is told when the panel queued it, :func:`_stuck`). The
**promotion check** is queued the same way (decision D63,
2026-11-12: what the dashboard said of each proxy's sponsor was five days old, read before the bot's
titles were understood, and two proxies had never been read): an ``inventory`` once per
:data:`CHECK_EVERY`, and :data:`AUTO_AFTER` after a sponsor was set (:func:`auto_inventory_due`).
What a finished inventory read is then acted on (:func:`repair_promotions`): a live proxy whose
screen names no sponsor, or another one, while the panel holds the wanted one as set, has its
sponsor set again by the sponsor loop (at most once per :data:`REPAIR_GAP` each); if the next read
still does not show it, the owner is told (at most once per :data:`ALERT_GAP` each). A read taken
before the sponsor was last set, or a reply that does not name the proxy (not its screen), is never
acted on.

The jobs:

* ``inventory`` (mode ``look``): read every page of /myproxies; the read replaces the entries
  (table ``mtproxybot_entries``). Then open the screen of each LIVE proxy of ours, one per pass,
  and keep the Proxy Sponsor it names (the promotion check). Nothing is ever pressed but a list
  button and a strict "next page" button.
* ``cleanup``: read the list again first, so every decision rests on what the bot shows now; then
  - mode ``observe`` (always, until removal is turned on, :func:`set_removal`): open the screen of
    ONE removable proxy and record what it looks like; nothing is pressed on it;
  - mode ``remove``: remove each removable proxy (node deleted, :func:`proxyhub.sponsor.botlist.classify`),
    one per pass, with the removal permit of the operator client naming that proxy alone; then read
    the list once more, which shows whether they are really gone.

A step runs from the sponsor loop's pass only when it had nothing else to do (a live proxy's
registration or sponsor always comes first; proxyhub/reconcilers/sponsor.py), with the kill switch
OFF, the operator account connected, and the hour's message budget holding the step plus
:data:`RESERVE` more. A removal also waits while conversations are paused (two stuck flows). Under
the operator account's lock (:func:`proxyhub.sponsor.service.with_bot_chat`), every message takes a
pacing slot (5 s gap, 30 an hour, FLOOD_WAIT honoured) and is logged before it goes out.

A walk that meets anything it does not know STOPS the job (``stuck``) and keeps the screens' shapes
(no secret) for the dashboard and the CLI. Nothing is retried blindly: a removal interrupted after
its removal button was pressed is never resumed by itself, and a transient failure (no reply,
Telegram unreachable) is tried again at most :data:`MAX_FAILURES` times, only before any removal
button was pressed.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.cloudflare.service import node_dns_name
from proxyhub.db.models import MtproxyRegistration, Setting
from proxyhub.db.models_sponsor import MtproxybotEntry
from proxyhub.fleet.actions import iso
from proxyhub.mtproto import settings as mtproto_settings
from proxyhub.mtproxy import state as account_state
from proxyhub.mtproxy import store as account_store
from proxyhub.mtproxy.userclient import BotChat, MtpError
from proxyhub.sponsor import botlist, driver, pacing, replies, store
from proxyhub.sponsor.botlist import (
    GONE,
    LIVE,
    EntryFacts,
    ListRead,
    Mapped,
    MenuSeen,
    MenuWalk,
    Removed,
    WalkStuck,
)
from proxyhub.sponsor.conversation import MESSAGES_NEEDED, PromoteJob, RegisterJob, Step
from proxyhub.sponsor.messages import (
    auto_job_stuck_alert,
    cleanup_stuck_alert,
    list_reason,
    promotion_alert,
)
from proxyhub.sponsor.provider import node_of
from proxyhub.sponsor.service import NOT_COUNTED, Deps, _log, in_use, with_bot_chat
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.store import enqueue_owner_alert

log = logging.getLogger(__name__)

KEY: Final = "sponsor.mtproxybot"
INVENTORY: Final = "inventory"
CLEANUP: Final = "cleanup"
LOOK: Final = "look"
OBSERVE: Final = "observe"
REMOVE: Final = "remove"
QUEUED: Final = "queued"
RUNNING: Final = "running"
DONE: Final = "done"
STUCK: Final = "stuck"
STOPPED: Final = "stopped"
FAILED: Final = "failed"
OPEN_STATES: Final = frozenset({QUEUED, RUNNING})
KINDS: Final = frozenset({INVENTORY, CLEANUP})
MODES: Final = frozenset({LOOK, OBSERVE, REMOVE})
STATES: Final = frozenset({QUEUED, RUNNING, DONE, STUCK, STOPPED, FAILED})
MAX_FAILURES: Final = 3
MAX_SCREENS: Final = 30
# One registration and one sponsor change stay possible this hour.
RESERVE: Final = MESSAGES_NEEDED[RegisterJob] + MESSAGES_NEEDED[PromoteJob]
REMOVAL_STEPS: Final = frozenset({Step.PRESSED_REMOVE, Step.PRESSED_CONFIRM})
# The observation that allows turning removal on: the dead proxy's screen named it and had exactly
# one button of the removal shape.
OBSERVED_OK: Final = "remove_button_found"
# The cleanup the panel queues by itself (module doc).
AUTO_ACTOR: Final = "sponsor:auto"
AUTO_AFTER: Final = timedelta(minutes=10)
AUTO_GAP: Final = timedelta(hours=1)
# The promotion check the panel queues by itself, and what it does with a finished one (D63).
CHECK_EVERY: Final = timedelta(hours=24)
CHECK_RETRY: Final = timedelta(hours=6)  # after a check that did not finish
REPAIR_GAP: Final = timedelta(hours=24)  # one proxy's sponsor is set again at most this often
ALERT_GAP: Final = timedelta(days=7)
REPAIRABLE: Final = frozenset({"not_shown", "other"})  # "several" is a screen the panel misreads
MAX_REPAIRS: Final = 200


class InventoryError(Exception):
    """An API or CLI error code (Persian text in proxyhub/sponsor/messages.py)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the job and its settings (settings row KEY) -----------------------------------------------------------


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _str(value: Any, limit: int = 120) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Job:
    kind: str
    mode: str
    state: str = QUEUED
    requested_by: str = ""
    requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    list_read_at: datetime | None = None
    relisted: bool = False
    observed: str | None = None
    removed: int = 0
    failures: int = 0
    reason: str | None = None
    step: str | None = None
    last_error: str | None = None

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        for name in ("requested_at", "started_at", "finished_at", "list_read_at"):
            out[name] = iso(out[name])
        return out

    @classmethod
    def from_json(cls, value: Any) -> Job | None:
        if not isinstance(value, dict) or value.get("kind") not in KINDS or value.get("mode") not in MODES:
            return None
        state = value.get("state") if value.get("state") in STATES else FAILED
        return cls(
            kind=str(value["kind"]),
            mode=str(value["mode"]),
            state=str(state),
            requested_by=_str(value.get("requested_by")) or "",
            requested_at=_time(value.get("requested_at")),
            started_at=_time(value.get("started_at")),
            finished_at=_time(value.get("finished_at")),
            list_read_at=_time(value.get("list_read_at")),
            relisted=value.get("relisted") is True,
            observed=_str(value.get("observed"), 260),
            removed=_int(value.get("removed")),
            failures=_int(value.get("failures")),
            reason=_str(value.get("reason"), 80),
            step=_str(value.get("step"), 24),
            last_error=_str(value.get("last_error"), 60),
        )


@dataclass(frozen=True)
class ListInfo:
    read_at: datetime
    pages: int
    complete: bool
    note: str | None
    total: int

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "read_at": iso(self.read_at)}

    @classmethod
    def from_json(cls, value: Any) -> ListInfo | None:
        read_at = _time(value.get("read_at")) if isinstance(value, dict) else None
        if read_at is None or not isinstance(value, dict):
            return None
        return cls(
            read_at=read_at,
            pages=_int(value.get("pages")),
            complete=value.get("complete") is True,
            note=_str(value.get("note"), 60),
            total=_int(value.get("total")),
        )


@dataclass(frozen=True)
class Repair:
    """One registration whose sponsor the panel set again because the bot did not show it: when,
    and when the owner was last told that it did not help."""

    at: datetime
    alerted_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {"at": iso(self.at), "alerted_at": iso(self.alerted_at)}

    @classmethod
    def from_json(cls, value: Any) -> Repair | None:
        at = _time(value.get("at")) if isinstance(value, dict) else None
        if at is None or not isinstance(value, dict):
            return None
        return cls(at=at, alerted_at=_time(value.get("alerted_at")))


def _repairs(value: Any) -> dict[str, Repair]:
    if not isinstance(value, dict):
        return {}
    found = {str(k)[:40]: Repair.from_json(v) for k, v in value.items()}
    return {k: v for k, v in list(found.items())[:MAX_REPAIRS] if v is not None}


@dataclass(frozen=True)
class BotListState:
    """``removal_enabled``: real removal is on (only :func:`set_removal`, after an observation).
    ``observation``: what the latest observe run saw on a dead proxy's screen. ``screens``: the
    current job's screens as shapes (no secret), at most :data:`MAX_SCREENS`. ``check_queued_at``
    and ``checked_at``: when an inventory was last queued and last finished ``done`` (the promotion
    check). ``cleanup_queued_at`` and ``cleaned_at``: when a removing cleanup was last queued, and
    when the last one that finished ``done`` was queued (what it covered). ``repaired_job``: the
    ``requested_at`` of the inventory whose reads were acted on. ``repairs``: by registration id
    (:class:`Repair`)."""

    removal_enabled: bool = False
    job: Job | None = None
    last_list: ListInfo | None = None
    observation: dict[str, Any] | None = None
    screens: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    check_queued_at: datetime | None = None
    checked_at: datetime | None = None
    cleanup_queued_at: datetime | None = None
    cleaned_at: datetime | None = None
    repaired_job: datetime | None = None
    repairs: dict[str, Repair] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "removal_enabled": self.removal_enabled,
            "job": self.job.to_json() if self.job is not None else None,
            "last_list": self.last_list.to_json() if self.last_list is not None else None,
            "observation": self.observation,
            "screens": list(self.screens),
            "check_queued_at": iso(self.check_queued_at),
            "checked_at": iso(self.checked_at),
            "cleanup_queued_at": iso(self.cleanup_queued_at),
            "cleaned_at": iso(self.cleaned_at),
            "repaired_job": iso(self.repaired_job),
            "repairs": {k: v.to_json() for k, v in self.repairs.items()},
        }

    @classmethod
    def from_json(cls, value: Any) -> BotListState:
        """A stored row, read defensively: anything unreadable falls back to its default."""
        if not isinstance(value, dict):
            return cls()
        screens = value.get("screens")
        observation = value.get("observation")
        return cls(
            removal_enabled=value.get("removal_enabled") is True,
            job=Job.from_json(value.get("job")),
            last_list=ListInfo.from_json(value.get("last_list")),
            observation=observation if isinstance(observation, dict) else None,
            screens=tuple(s for s in screens if isinstance(s, dict))[-MAX_SCREENS:]
            if isinstance(screens, list)
            else (),
            check_queued_at=_time(value.get("check_queued_at")),
            checked_at=_time(value.get("checked_at")),
            cleanup_queued_at=_time(value.get("cleanup_queued_at")),
            cleaned_at=_time(value.get("cleaned_at")),
            repaired_job=_time(value.get("repaired_job")),
            repairs=_repairs(value.get("repairs")),
        )


async def load_state(db: AsyncConnection, *, for_update: bool = False) -> BotListState:
    stmt = select(Setting.value).where(Setting.key == KEY)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return BotListState() if row is None else BotListState.from_json(row[0])


async def save_state(db: AsyncConnection, value: BotListState) -> None:
    stmt = pg_insert(Setting).values(key=KEY, value=value.to_json())
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": text("now()")}
        )
    )


async def _locked_state(db: AsyncConnection) -> BotListState:
    await db.execute(
        pg_insert(Setting)
        .values(key=KEY, value=BotListState().to_json())
        .on_conflict_do_nothing(index_elements=["key"])
    )
    return await load_state(db, for_update=True)


# --- the entries (table mtproxybot_entries) -----------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    id: uuid.UUID
    host: str
    port: int | None
    label_shape: str
    label_fp: str
    ordinal: int
    tag_registration_id: uuid.UUID | None
    page: int
    position: int
    pressable: bool
    first_seen_at: datetime
    seen_at: datetime
    menu_read_at: datetime | None
    menu_channel: str | None
    menu_note: str | None
    removal_state: str | None
    removal_reason: str | None
    removal_at: datetime | None

    @property
    def address(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"

    def facts(self) -> EntryFacts:
        return EntryFacts(
            host=self.host,
            port=self.port,
            label_fp=self.label_fp,
            tag_registration_id=self.tag_registration_id,
        )


_E = MtproxybotEntry.__table__.c


async def load_entries(db: AsyncConnection, *, for_update: bool = False) -> list[Entry]:
    stmt = select(MtproxybotEntry.__table__).order_by(_E.page, _E.position, _E.id)
    if for_update:
        stmt = stmt.with_for_update()
    return [Entry(**dict(r._mapping)) for r in (await db.execute(stmt)).all()]


CARRIED: Final = (
    "first_seen_at",
    "menu_read_at",
    "menu_channel",
    "menu_note",
    "removal_state",
    "removal_reason",
    "removal_at",
)


async def replace_entries(
    db: AsyncConnection, read: ListRead, regs: Sequence[store.Reg], now: datetime
) -> dict[str, int]:
    """The list just read replaces the stored one. What the panel learnt about a button (its
    screen, a removal) follows it by ``(label fingerprint, nth button with that label)``. The
    label's hex words only serve to find the registration whose tag they name; they are not kept.
    Returns how many buttons a removal was reported for are gone now, and how many are still
    listed."""
    old = {(e.label_fp, e.ordinal): e for e in await load_entries(db, for_update=True)}
    tags = {r.ad_tag: r.registration_id for r in regs if r.ad_tag and r.registration_id is not None}
    await db.execute(delete(MtproxybotEntry))
    seen: dict[str, int] = {}
    kept: set[tuple[str, int]] = set()
    for item in read.entries:
        ordinal = seen.get(item.label_fp, 0)
        seen[item.label_fp] = ordinal + 1
        prev = old.get((item.label_fp, ordinal))
        kept.add((item.label_fp, ordinal))
        hint = replies.tag_hint(item.hex_words, tags)
        carried: dict[str, Any] = (
            {name: getattr(prev, name) for name in CARRIED} if prev is not None else {"first_seen_at": now}
        )
        await db.execute(
            insert(MtproxybotEntry).values(
                id=uuid.uuid4(),
                host=item.host[:253],
                port=item.port,
                label_shape=item.label_shape[:200],
                label_fp=item.label_fp,
                ordinal=ordinal,
                tag_registration_id=tags.get(hint) if hint is not None else None,
                page=item.page,
                position=item.position,
                pressable=item.pressable,
                seen_at=now,
                **carried,
            )
        )
    reported = {k for k, e in old.items() if e.removal_state in ("removed", "unconfirmed")}
    return {"removal_confirmed": len(reported - kept), "removal_still_listed": len(reported & kept)}


async def update_entry(db: AsyncConnection, entry_id: uuid.UUID, **values: Any) -> None:
    await db.execute(update(MtproxybotEntry).where(_E.id == entry_id).values(**values))


# --- what to do next (pure) ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """``kind``: ``list``, ``relist``, ``menu``, ``observe``, ``remove``, ``wait`` (removal while
    paused) or ``finish`` (with ``state`` and ``reason``). ``cost``: messages it may need."""

    kind: str
    entry: Entry | None = None
    mapped: Mapped | None = None
    state: str | None = None
    reason: str | None = None
    cost: int = 0


def _since(moment: datetime | None, job: Job) -> bool:
    return moment is not None and job.started_at is not None and moment >= job.started_at


def list_cost(info: ListInfo | None) -> int:
    """/myproxies and one press per further page (two pages assumed before the first read)."""
    return max(info.pages if info is not None else 2, 1)


def plan(job: Job, info: ListInfo | None, items: Sequence[tuple[Entry, Mapped]], *, paused: bool) -> Plan:
    if not job.open:
        return Plan("finish", state=job.state, reason=job.reason)
    if job.list_read_at is None:
        return Plan("list", cost=list_cost(info))
    if job.kind == INVENTORY:
        for entry, mapped in items:
            if (
                mapped.status == LIVE
                and entry.pressable
                and (mapped.exact or mapped.listed == 1)
                and not _since(entry.menu_read_at, job)
            ):
                return Plan("menu", entry=entry, mapped=mapped, cost=entry.page + 1)
        return Plan("finish", state=DONE)
    removable = [(e, m) for e, m in items if m.removable and e.pressable]
    if job.mode == OBSERVE:
        if job.observed is not None:
            return Plan("finish", state=DONE)
        if not removable:
            return Plan("finish", state=DONE, reason="nothing_to_remove")
        entry, mapped = removable[0]
        return Plan("observe", entry=entry, mapped=mapped, cost=entry.page + 1)
    if paused:
        return Plan("wait", reason="paused")
    if info is None or not info.complete:
        return Plan("finish", state=STOPPED, reason="list_incomplete")
    for entry, mapped in removable:
        if not _since(entry.removal_at, job):
            return Plan("remove", entry=entry, mapped=mapped, cost=entry.page + 4)
    if job.removed > 0 and not job.relisted:
        return Plan("relist", cost=list_cost(info))
    return Plan("finish", state=DONE, reason=None if job.removed else "nothing_to_remove")


# --- one step ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    """``ports``: every port our proxies use or used, for an entry with no row at its address
    (:func:`proxyhub.sponsor.botlist.target_of`)."""

    state: BotListState
    plan: Plan
    copies: int
    live_tags: frozenset[str] = frozenset()
    ports: tuple[int, ...] = ()


def _copies(entries: Sequence[Entry], entry: Entry) -> int:
    return sum(1 for e in entries if e.label_fp == entry.label_fp)


async def our_ports(db: AsyncConnection, regs: Sequence[store.Reg]) -> tuple[int, ...]:
    """The ports of our registrations, the MTProto default and the port set now (the panel has only
    ever used 8443): what the screen of a proxy whose rows were deleted can name it by."""
    ports = {r.port for r in regs} | {mtproto_settings.DEFAULT_PORT}
    with contextlib.suppress(mtproto_settings.SettingsCorrupt):
        ports.add((await mtproto_settings.load_settings(db)).port)
    return tuple(sorted(ports))


def walk_target(prepared: _Prepared, entry: Entry, mapped: Mapped) -> botlist.Target:
    """The target of a walk to this entry's screen (and the removal permit's words)."""
    return botlist.target_of(
        entry.facts(),
        mapped,
        copies=prepared.copies,
        live_tags=prepared.live_tags,
        fallback_ports=prepared.ports,
    )


async def known_addresses(db: AsyncConnection, regs: Sequence[store.Reg]) -> botlist.Known:
    """:class:`proxyhub.sponsor.botlist.Known`: the IPv4s our servers ever had, our pool domains, and
    the addresses held now by a node that is not deleted or by a registration that is not retired."""
    ips = {str(r[0]) for r in await db.execute(text("SELECT DISTINCT host(ip) FROM ip_history"))}
    zones = {str(r[0]).lower() for r in await db.execute(text("SELECT name FROM cf_zones"))}
    held = {r.host.lower() for r in regs if r.state != store.RETIRED}
    rows = await db.execute(text("SELECT id, slot, host(ipv4) FROM nodes WHERE state <> 'deleted'"))
    for node_id, slot, ipv4 in rows:
        if ipv4 is not None:
            held.add(str(ipv4))
        held.update(node_dns_name(slot, node_id, zone) for zone in zones)
    return botlist.Known(ips=frozenset(ips), zones=frozenset(zones), held=frozenset(held))


def live_tags(regs: Sequence[store.Reg]) -> frozenset[str]:
    """The tags of our registrations that are not retired: a removal never touches their screen."""
    return frozenset(r.ad_tag for r in regs if r.ad_tag and r.state != store.RETIRED)


async def _context(
    db: AsyncConnection,
) -> tuple[list[store.Reg], dict[uuid.UUID, str], list[Entry], list[Mapped]]:
    regs = await store.load_regs(db)
    nodes = await store.node_states(db, {n for r in regs if (n := node_of(r.proxy_key)) is not None})
    entries = await load_entries(db)
    known = await known_addresses(db, regs)
    mapped = botlist.classify([e.facts() for e in entries], regs, nodes, known)
    return regs, nodes, entries, mapped


async def _prepare(deps: Deps, now: datetime, paused: bool) -> _Prepared | str:
    async with deps.engine.begin() as db:
        state = await _locked_state(db)
        job = state.job
        if job is None or not job.open:
            return "idle"
        if await account_store.kill_switch_on(db):
            return "kill_switch"
        account = await account_store.load_row(db)
        if account.state != account_state.CONNECTED or account.mtproxybot_id is None:
            return "no_account"
        regs, _, entries, mapped = await _context(db)
        step_plan = plan(job, state.last_list, list(zip(entries, mapped, strict=True)), paused=paused)
        if step_plan.kind == "wait":
            return "paused"
        if step_plan.kind == "finish":
            await _finish(db, state, step_plan.state or DONE, step_plan.reason, now)
            return f"mtproxybot:{step_plan.state}"
        if pacing.remaining(await pacing.load(db), now) < step_plan.cost + RESERVE:
            return "paced"
        if job.state == QUEUED:
            job = replace(job, state=RUNNING, started_at=now)
            state = replace(state, job=job, screens=())
            await save_state(db, state)
            await _log(
                db,
                "sponsor.mtproxybot_started",
                "@MTProxybot",
                reasons=[job.kind, job.mode],
                context={"requested_by": job.requested_by},
            )
        copies = _copies(entries, step_plan.entry) if step_plan.entry is not None else 0
        return _Prepared(
            state=state,
            plan=step_plan,
            copies=copies,
            live_tags=live_tags(regs),
            ports=await our_ports(db, regs),
        )


async def step(deps: Deps, *, paused: bool = False) -> str:
    """One step of the open job, if any (module docstring). Returns ``idle`` (no job),
    ``kill_switch``, ``no_account``, ``paused``, ``paced``, ``busy``, ``vault_unavailable``, or
    ``mtproxybot:<what happened>``."""
    now = deps.clock()
    prepared = await _prepare(deps, now, paused)
    if isinstance(prepared, str):
        return prepared
    the_plan = prepared.plan
    entry, mapped = the_plan.entry, the_plan.mapped
    machine: botlist.ListWalk | MenuWalk
    permit: frozenset[str] = frozenset()
    if entry is None or mapped is None:
        machine, target = botlist.ListWalk(), "@MTProxybot"
    else:
        to = walk_target(prepared, entry, mapped)
        machine, target = MenuWalk(to, remove=the_plan.kind == "remove"), entry.address
        if the_plan.kind == "remove":
            permit = to.words

    async def talk(
        chat: BotChat, take_slot: Callable[[], Awaitable[object]], recorder: driver.Recorder
    ) -> driver.WalkResult:
        if permit:
            chat.permit_removal(permit)
        return await driver.walk(
            chat,
            machine,
            take_slot=take_slot,
            record=recorder,
            timing=deps.timing,
            max_actions=botlist.MAX_WALK_ACTIONS,
            sleep=deps.sleep,
        )

    try:
        outcome: driver.WalkResult | str = await with_bot_chat(deps, target, talk)
    except MtpError as exc:
        outcome = driver.WalkResult(error=exc.code, rpc=exc.rpc, retry_after_s=exc.seconds)
    if isinstance(outcome, str):
        return outcome
    async with deps.engine.begin() as db:
        return await _record(db, prepared, outcome, deps.clock())


# --- recording an outcome -----------------------------------------------------------------------------------


def _screens(state: BotListState, found: Sequence[botlist.Screen], about: str, now: datetime) -> BotListState:
    added = [{**s.to_json(), "about": about, "at": iso(now)} for s in found]
    return replace(state, screens=(*state.screens, *added)[-MAX_SCREENS:])


def _same_job(current: BotListState, prepared: BotListState) -> bool:
    """The job the step was planned for is still the open one (the owner may have stopped it)."""
    a, b = current.job, prepared.job
    return a is not None and b is not None and a.open and a.requested_at == b.requested_at


async def _record(db: AsyncConnection, prepared: _Prepared, outcome: driver.WalkResult, now: datetime) -> str:
    """Entries are always updated (a removal that happened is a fact, even if the owner stopped
    the job meanwhile); the job moves only while it is still the one the step was for."""
    state = await _locked_state(db)
    same = _same_job(state, prepared.state)
    the_plan = prepared.plan
    entry = the_plan.entry
    result = outcome.result
    about = entry.address if entry is not None else "list"
    found = getattr(result, "screens", ()) or ()
    if same:
        state = _screens(state, found, about, now)
    if the_plan.kind in ("list", "relist"):
        return await _record_list(db, state, prepared, outcome, now, same)
    assert entry is not None
    if the_plan.kind == "menu":
        return await _record_menu(db, state, entry, outcome, now, same)
    if the_plan.kind == "observe":
        return await _record_observe(db, state, entry, outcome, now, same)
    return await _record_removal(db, state, entry, outcome, now, same)


async def _record_list(
    db: AsyncConnection,
    state: BotListState,
    prepared: _Prepared,
    outcome: driver.WalkResult,
    now: datetime,
    same: bool,
) -> str:
    result = outcome.result
    if isinstance(result, ListRead):
        regs = await store.load_regs(db)
        counts = await replace_entries(db, result, regs, now)
        info = ListInfo(
            read_at=now,
            pages=result.pages,
            complete=result.complete,
            note=result.note,
            total=len(result.entries),
        )
        state = replace(state, last_list=info)
        if same and state.job is not None:
            relist = prepared.plan.kind == "relist"
            job = replace(
                state.job,
                list_read_at=state.job.list_read_at if relist else now,
                relisted=state.job.relisted or relist,
                failures=0,
                last_error=None,
            )
            state = replace(state, job=job)
        await save_state(db, state)
        _, _, _, mapped = await _context(db)
        by_status: dict[str, int] = {}
        for m in mapped:
            by_status[m.status] = by_status.get(m.status, 0) + 1
        await _log(
            db,
            "sponsor.mtproxybot_list_read",
            "@MTProxybot",
            reasons=["complete" if result.complete else (result.note or "incomplete")],
            context={"pages": result.pages, "total": len(result.entries), **by_status, **counts},
        )
        return "mtproxybot:list"
    if isinstance(result, WalkStuck):
        return await _stuck(
            db, state, "@MTProxybot", result.step.value, result.reason, now, same, alert=False
        )
    return await _transient(db, state, "@MTProxybot", outcome, now, same)


async def _record_menu(
    db: AsyncConnection,
    state: BotListState,
    entry: Entry,
    outcome: driver.WalkResult,
    now: datetime,
    same: bool,
) -> str:
    result = outcome.result
    if isinstance(result, MenuSeen) and not result.names_proxy:
        # Not the proxy's screen (a toast, an error text, the list again): a read error, never
        # "no sponsor shown", which the promotion repair would act on.
        result = WalkStuck(step=Step.PRESSED_PROXY, reason="menu_does_not_name_proxy", screens=result.screens)
    if isinstance(result, MenuSeen):
        channels = result.channels
        note = "shown" if len(channels) == 1 else ("several" if channels else "not_shown")
        if not channels and result.promoted_title:
            # The bot names the sponsor by its title (P26): kept, compared with the wanted
            # channel's title when the owner's view is built (:func:`_promotion`).
            note = f"title:{replies.title_key(result.promoted_title)}"
        await update_entry(
            db,
            entry.id,
            menu_read_at=now,
            menu_channel=channels[0][:32] if len(channels) == 1 else None,
            menu_note=note,
        )
        if same:
            await save_state(db, state)
        await _log(
            db,
            "sponsor.mtproxybot_menu_read",
            entry.address,
            reasons=[note],
            context={"channel": f"@{channels[0]}" if len(channels) == 1 else None, "channels": len(channels)},
        )
        return "mtproxybot:menu"
    if isinstance(result, WalkStuck):
        # Read only: this proxy's screen stays unknown and the inventory goes on with the next one.
        await update_entry(
            db, entry.id, menu_read_at=now, menu_channel=None, menu_note=f"stuck:{result.reason}"[:60]
        )
        if same:
            await save_state(db, state)
        await _log(
            db,
            "sponsor.mtproxybot_menu_read",
            entry.address,
            allowed=False,
            reasons=[result.reason],
            context={"step": result.step.value},
        )
        return f"mtproxybot:menu_stuck:{result.reason}"
    return await _transient(db, state, entry.address, outcome, now, same)


async def _record_observe(
    db: AsyncConnection,
    state: BotListState,
    entry: Entry,
    outcome: driver.WalkResult,
    now: datetime,
    same: bool,
) -> str:
    result = outcome.result
    if isinstance(result, MenuSeen):
        if result.remove_named:
            reason = OBSERVED_OK
        elif result.remove_buttons == 0:
            reason = "no_remove_button"
        elif result.remove_buttons > 1:
            reason = "several_remove_buttons"
        else:
            reason = "menu_does_not_name_proxy"
        await update_entry(db, entry.id, removal_state="observed", removal_reason=reason, removal_at=now)
        observation = {
            "at": iso(now),
            "address": entry.address,
            "remove_buttons": result.remove_buttons,
            "names_proxy": result.names_proxy,
            "remove_named": result.remove_named,
            "result": reason,
        }
        state = replace(state, observation=observation)
        if same and state.job is not None:
            state = replace(
                state, job=replace(state.job, observed=entry.address, failures=0, last_error=None)
            )
        await save_state(db, state)
        await _log(
            db,
            "sponsor.mtproxybot_observed",
            entry.address,
            reasons=[reason],
            context={"remove_buttons": result.remove_buttons, "names_proxy": result.names_proxy},
        )
        return "mtproxybot:observed"
    if isinstance(result, WalkStuck):
        await update_entry(db, entry.id, removal_state="stuck", removal_reason=result.reason, removal_at=now)
        return await _stuck(
            db, state, entry.address, result.step.value, result.reason, now, same, alert=False
        )
    return await _transient(db, state, entry.address, outcome, now, same)


async def _record_removal(
    db: AsyncConnection,
    state: BotListState,
    entry: Entry,
    outcome: driver.WalkResult,
    now: datetime,
    same: bool,
) -> str:
    result = outcome.result
    if isinstance(result, Removed):
        reason = "confirmed" if result.confirmed else "removed_at_once"
        await update_entry(db, entry.id, removal_state="removed", removal_reason=reason, removal_at=now)
        if same and state.job is not None:
            state = replace(
                state, job=replace(state.job, removed=state.job.removed + 1, failures=0, last_error=None)
            )
        await save_state(db, state)
        await _log(
            db, "sponsor.mtproxybot_removed", entry.address, reasons=[reason], context={"page": entry.page}
        )
        return "mtproxybot:removed"
    if isinstance(result, WalkStuck):
        await update_entry(db, entry.id, removal_state="stuck", removal_reason=result.reason, removal_at=now)
        return await _stuck(db, state, entry.address, result.step.value, result.reason, now, same, alert=True)
    if outcome.attempted_step in REMOVAL_STEPS:
        # A removal button may have reached the bot: never resumed by itself.
        code = outcome.error or "unknown"
        await update_entry(
            db, entry.id, removal_state="interrupted", removal_reason=code[:60], removal_at=now
        )
        step_name = outcome.attempted_step.value if outcome.attempted_step is not None else None
        if outcome.error == "flood_wait" and outcome.retry_after_s:
            await pacing.hold(db, now + timedelta(seconds=outcome.retry_after_s + 5), now)
        return await _stuck(db, state, entry.address, step_name, f"interrupted:{code}", now, same, alert=True)
    if outcome.error == driver.REFUSED:  # refused before it left: nothing was taken away
        await update_entry(db, entry.id, removal_state="stuck", removal_reason=driver.REFUSED, removal_at=now)
        last = outcome.last_step.value if outcome.last_step is not None else None
        return await _stuck(db, state, entry.address, last, driver.REFUSED, now, same, alert=True)
    return await _transient(db, state, entry.address, outcome, now, same)


async def _stuck(
    db: AsyncConnection,
    state: BotListState,
    target: str,
    step_name: str | None,
    reason: str,
    now: datetime,
    same: bool,
    *,
    alert: bool,
) -> str:
    """The walk met something unknown: the job stops here (fail-closed). A stuck removal alerts the
    owner (``alert``); so does any stuck job the panel queued by itself, since no automatic job
    starts again until the owner starts one (:func:`auto_cleanup_due`, :func:`auto_inventory_due`)
    and nobody is looking at the page."""
    auto_kind: str | None = None
    if same and state.job is not None:
        if state.job.requested_by == AUTO_ACTOR:
            auto_kind = state.job.kind
        state = replace(
            state, job=replace(state.job, state=STUCK, reason=reason[:80], step=step_name, finished_at=now)
        )
    await save_state(db, state)
    await _log(
        db,
        "sponsor.mtproxybot_stuck",
        target,
        allowed=False,
        reasons=[reason],
        context={"step": step_name},
    )
    if alert:
        await enqueue_owner_alert(db, cleanup_stuck_alert(address=target, step=step_name, reason=reason))
    elif auto_kind is not None:
        await enqueue_owner_alert(
            db, auto_job_stuck_alert(kind=auto_kind, target=target, step=step_name, reason=reason)
        )
    return f"mtproxybot:stuck:{reason}"


async def _transient(
    db: AsyncConnection,
    state: BotListState,
    target: str,
    outcome: driver.WalkResult,
    now: datetime,
    same: bool,
) -> str:
    """Telegram, the pace or the bot's silence (no removal button was pressed): tried again on a
    later pass, at most :data:`MAX_FAILURES` times. A request the operator client refused is no
    such failure: the walk and the allowlist disagree, so the job stops."""
    code = outcome.error or "unknown"
    if code == driver.REFUSED:
        step_name = outcome.last_step.value if outcome.last_step is not None else None
        return await _stuck(db, state, target, step_name, code, now, same, alert=False)
    if code == "flood_wait" and outcome.retry_after_s:
        await pacing.hold(db, now + timedelta(seconds=outcome.retry_after_s + 5), now)
    failed = False
    if same and state.job is not None and code not in NOT_COUNTED:
        failures = state.job.failures + 1
        failed = failures >= MAX_FAILURES
        state = replace(
            state,
            job=replace(
                state.job,
                failures=failures,
                last_error=code[:60],
                state=FAILED if failed else state.job.state,
                reason=code[:80] if failed else state.job.reason,
                finished_at=now if failed else None,
            ),
        )
    await save_state(db, state)
    context: dict[str, Any] = {"messages_sent": outcome.messages_sent}
    if outcome.rpc:
        context["telegram_error"] = outcome.rpc
    await _log(db, "sponsor.mtproxybot_failed", target, allowed=False, reasons=[code], context=context)
    if failed:
        await _log(db, "sponsor.mtproxybot_finished", "@MTProxybot", allowed=False, reasons=[FAILED, code])
    return f"mtproxybot:failed:{code}"


async def _finish(
    db: AsyncConnection, state: BotListState, final: str, reason: str | None, now: datetime
) -> None:
    if state.job is None:
        return
    job = replace(state.job, state=final, reason=reason, finished_at=now)
    checked = now if job.kind == INVENTORY and final == DONE else state.checked_at
    # Every registration retired before this cleanup was queued was in the list it walked.
    cleaned = (
        job.requested_at
        if job.kind == CLEANUP and job.mode == REMOVE and final == DONE and job.requested_at is not None
        else state.cleaned_at
    )
    await save_state(db, replace(state, job=job, checked_at=checked, cleaned_at=cleaned))
    await _log(
        db,
        "sponsor.mtproxybot_finished",
        "@MTProxybot",
        allowed=final == DONE,
        reasons=[final, *([reason] if reason else [])],
        context={"kind": job.kind, "mode": job.mode, "removed": job.removed},
    )


# --- the owner's and the operator's actions ---------------------------------------------------------------


async def request_job(engine: AsyncEngine, kind: str, *, actor: str, now: datetime) -> Job:
    """Queue an ``inventory`` or a ``cleanup`` (observe until removal is on). One job at a time;
    refused while the kill switch is ON or the account is not connected (the loop checks again)."""
    if kind not in KINDS:
        raise InventoryError("invalid_request")
    async with engine.begin() as db:
        state = await _locked_state(db)
        if state.job is not None and state.job.open:
            raise InventoryError("mtproxybot_job_running")
        if await account_store.kill_switch_on(db):
            raise InventoryError("mtproxybot_kill_switch")
        account = await account_store.load_row(db)
        if account.state != account_state.CONNECTED or account.mtproxybot_id is None:
            raise InventoryError("mtproxybot_no_account")
        mode = LOOK if kind == INVENTORY else (REMOVE if state.removal_enabled else OBSERVE)
        job = Job(kind=kind, mode=mode, requested_by=actor[:120], requested_at=now)
        queued = now if kind == INVENTORY else state.check_queued_at
        cleanup_queued = now if kind == CLEANUP and mode == REMOVE else state.cleanup_queued_at
        await save_state(
            db, replace(state, job=job, check_queued_at=queued, cleanup_queued_at=cleanup_queued)
        )
        await _log(db, "sponsor.mtproxybot_requested", "@MTProxybot", reasons=[kind, mode], actor=actor)
    return job


async def auto_cleanup_due(db: AsyncConnection, now: datetime) -> bool:
    """Whether the panel should queue a ``cleanup`` by itself now (see the module doc): a
    registration retired as ``node_gone`` at least :data:`AUTO_AFTER` ago that no cleanup covered
    yet. Due at once when it was retired after the last cleanup was queued; one that a cleanup was
    already queued for, which did not finish ``done``, is offered again :data:`CHECK_RETRY` later.
    An inventory never moves these marks (the promotion check is no cleanup)."""
    state = await load_state(db)
    if not state.removal_enabled:
        return False
    if state.job is not None:
        if state.job.open or state.job.state == STUCK:
            return False
        last = state.job.finished_at or state.job.requested_at
        if last is not None and now - last < AUTO_GAP:
            return False
    newest = select(func.max(MtproxyRegistration.updated_at)).where(
        MtproxyRegistration.state == "retired",
        MtproxyRegistration.state_reason == "node_gone",
        MtproxyRegistration.updated_at <= now - AUTO_AFTER,
    )
    if state.cleaned_at is not None:
        newest = newest.where(MtproxyRegistration.updated_at > state.cleaned_at)
    gone_at = (await db.execute(newest)).scalar_one()
    if gone_at is None:
        return False
    queued = state.cleanup_queued_at
    return queued is None or gone_at > queued or now - queued >= CHECK_RETRY


def _checkable(reg: store.Reg, nodes: dict[uuid.UUID, str]) -> bool:
    """A registered proxy a live node serves now, whose flow is not stuck (the owner looks at a
    stuck one first)."""
    return reg.state == store.REGISTERED and reg.ad_tag is not None and not reg.stuck and in_use(reg, nodes)


async def auto_inventory_due(db: AsyncConnection, now: datetime) -> bool:
    """Whether the panel should queue an ``inventory`` (the promotion check) by itself now (module
    doc): a sponsor was set since the last one was queued, or the last finished one is older than
    :data:`CHECK_EVERY` (tried again :data:`CHECK_RETRY` after one that did not finish)."""
    state = await load_state(db)
    if state.job is not None:
        if state.job.open or state.job.state == STUCK:
            return False
        last = state.job.finished_at or state.job.requested_at
        if last is not None and now - last < AUTO_GAP:
            return False
    regs = await store.load_regs(db)
    nodes = await store.node_states(db, {n for r in regs if (n := node_of(r.proxy_key)) is not None})
    live = [r for r in regs if _checkable(r, nodes)]
    if not live:
        return False
    queued = state.check_queued_at
    for reg in live:
        at = reg.promotion_set_at
        if at is not None and at <= now - AUTO_AFTER and (queued is None or at > queued):
            return True
    if state.checked_at is not None and now - state.checked_at < CHECK_EVERY:
        return False
    return queued is None or now - queued >= CHECK_RETRY


def _read_job(state: BotListState) -> Job | None:
    """The finished inventory whose reads were not acted on yet, if any."""
    job = state.job
    if job is None or job.kind != INVENTORY or job.state != DONE or job.requested_at is None:
        return None
    return None if state.repaired_job == job.requested_at else job


async def repair_promotions(engine: AsyncEngine, *, wanted: str, now: datetime) -> list[str]:
    """Act once on what the last finished inventory read (module doc). Returns the addresses whose
    sponsor will be set again: their registration loses its ``sponsor_channel``, which is what the
    owner's «ثبت دوباره» does, so the sponsor loop promotes them on its next passes."""
    async with engine.connect() as db:
        if _read_job(await load_state(db)) is None:  # the usual pass: nothing new was read
            return []
    async with engine.begin() as db:
        state = await _locked_state(db)
        job = _read_job(state)
        if job is None:
            return []
        regs, nodes, entries, mapped = await _context(db)
        wanted_title = await _channel_title(db, wanted)
        name = wanted.lstrip("@").lower()
        held = {str(r.registration_id) for r in regs if r.registration_id and r.state != store.RETIRED}
        repairs = {k: v for k, v in state.repairs.items() if k in held}
        again: list[str] = []
        for entry, found in zip(entries, mapped, strict=True):
            reg = found.regs[0] if found.regs else None
            if reg is None or reg.registration_id is None or not _since(entry.menu_read_at, job):
                continue
            if (
                reg.promotion_set_at is not None
                and entry.menu_read_at is not None
                and entry.menu_read_at <= reg.promotion_set_at
            ):
                # The screen was read before the sponsor was last set (the sponsor loop goes first
                # and may set it during the check): the next check, queued AUTO_AFTER after that
                # set, reads it again and is the one acted on.
                continue
            key = str(reg.registration_id)
            promotion = _promotion(entry, found, wanted, wanted_title)
            if promotion == "ok":
                repairs.pop(key, None)
                continue
            if promotion not in REPAIRABLE or not _checkable(reg, nodes):
                continue
            if (reg.sponsor_channel or "").lstrip("@").lower() != name:
                continue  # the loop is about to set it anyway (a campaign's start or end)
            last = repairs.get(key)
            if last is not None and now - last.at < REPAIR_GAP:
                # Set again not long ago and the bot still does not show it: the owner is told.
                if last.alerted_at is None or now - last.alerted_at >= ALERT_GAP:
                    alert = promotion_alert(address=reg.address, promotion=promotion, wanted=name)
                    if await enqueue_owner_alert(db, alert) is not None:
                        repairs[key] = replace(last, alerted_at=now)
                        await _log(
                            db,
                            "sponsor.promotion_alert",
                            reg.address,
                            allowed=False,
                            reasons=[promotion],
                            actor=AUTO_ACTOR,
                        )
                continue
            await store.update_reg(db, reg.registration_id, now, sponsor_channel=None)
            await store.update_flow(db, reg.id, now, attempts=0, next_attempt_at=None, last_error=None)
            repairs[key] = Repair(at=now, alerted_at=last.alerted_at if last is not None else None)
            again.append(reg.address)
            await _log(
                db,
                "sponsor.promotion_repair",
                reg.address,
                reasons=[promotion],
                context={"wanted": f"@{name}", "read_at": iso(entry.menu_read_at)},
                actor=AUTO_ACTOR,
            )
        await save_state(db, replace(state, repairs=repairs, repaired_job=job.requested_at))
    return again


async def stop_job(engine: AsyncEngine, *, actor: str, now: datetime) -> None:
    """The open job ends now (a walk already under way finishes its current step only)."""
    async with engine.begin() as db:
        state = await _locked_state(db)
        if state.job is None or not state.job.open:
            raise InventoryError("mtproxybot_no_job")
        job = replace(state.job, state=STOPPED, reason="owner", finished_at=now)
        await save_state(db, replace(state, job=job))
        await _log(db, "sponsor.mtproxybot_finished", "@MTProxybot", reasons=[STOPPED, "owner"], actor=actor)


async def set_removal(engine: AsyncEngine, enabled: bool, *, actor: str, now: datetime) -> BotListState:
    """Real removal on or off (the CLI only; the dashboard shows it). ON needs an observation of a
    dead proxy's screen that named it and had exactly one button of the removal shape."""
    async with engine.begin() as db:
        state = await _locked_state(db)
        observed = (state.observation or {}).get("result")
        if enabled and observed != OBSERVED_OK:
            raise InventoryError("mtproxybot_not_observed")
        state = replace(state, removal_enabled=enabled)
        await save_state(db, state)
        await _log(
            db,
            "sponsor.mtproxybot_removal_setting",
            "@MTProxybot",
            reasons=["on" if enabled else "off"],
            context={"observation": state.observation, "at": iso(now)},
            actor=actor,
        )
    return state


# --- the owner's view ------------------------------------------------------------------------------------


def _promotion(entry: Entry, mapped: Mapped, wanted: str, wanted_title: str | None = None) -> str | None:
    """For a live proxy: ``ok`` (its screen names the wanted channel, by @name or, as the bot does,
    by the title our stored channel has), ``other`` (another one), ``not_shown`` (its screen names
    no channel), ``several``, ``stuck`` (its screen could not be read) or ``unread``. None for a
    proxy that is not live."""
    if mapped.status != LIVE:
        return None
    if entry.menu_read_at is None:
        return "unread"
    if entry.menu_channel is not None:
        return "ok" if entry.menu_channel == wanted else "other"
    note = entry.menu_note or ""
    if note.startswith("title:"):
        if wanted_title and note[len("title:") :] == replies.title_key(wanted_title):
            return "ok"
        # The stored title may be older than the channel's (the owner renamed @ipoxxo, P26): a
        # sponsor shown by title on a proxy our flow promoted to the wanted channel counts too.
        reg = mapped.regs[0] if mapped.regs else None
        set_to = ((reg.sponsor_channel if reg is not None else None) or "").lstrip("@").lower()
        return "ok" if set_to and set_to == wanted.lstrip("@").lower() else "other"
    return "stuck" if note.startswith("stuck:") else (note if note in ("not_shown", "several") else "unread")


def _entry_view(
    entry: Entry,
    mapped: Mapped,
    nodes: dict[uuid.UUID, str],
    wanted: str,
    wanted_title: str | None = None,
) -> dict[str, Any]:
    reg = mapped.regs[0] if mapped.regs else None
    node = node_of(reg.proxy_key) if reg is not None else None
    return {
        "id": str(entry.id),
        "address": entry.address,
        "host": entry.host,
        "port": entry.port,
        "label_shape": entry.label_shape,
        "page": entry.page,
        "listed": mapped.listed,
        "status": mapped.status,
        "exact": mapped.exact,
        "removable": mapped.removable and entry.pressable,
        "note": mapped.note,
        "registration": None
        if reg is None
        else {
            "address": reg.address,
            "state": reg.state,
            "state_reason": reg.state_reason,
            "node_state": nodes.get(node, "deleted") if node is not None else None,
        },
        "sponsor_channel": entry.menu_channel,
        "menu_read_at": iso(entry.menu_read_at),
        "promotion": _promotion(entry, mapped, wanted, wanted_title),
        "removal_state": entry.removal_state,
        "removal_reason": entry.removal_reason,
        "removal_reason_fa": list_reason(entry.removal_reason)
        if entry.removal_state in ("stuck", "interrupted")
        else None,
        "removal_at": iso(entry.removal_at),
        "first_seen_at": iso(entry.first_seen_at),
    }


def _job_view(job: Job | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        **job.to_json(),
        "reason_fa": list_reason(job.reason),
        "last_error_fa": list_reason(job.last_error),
    }


async def _channel_title(db: AsyncConnection, username: str) -> str | None:
    """The title the panel stored for one of OUR channels with this @name (the bot shows a sponsor
    by its title); None for a channel that is not ours (a campaign's)."""
    name = username.lstrip("@").lower()
    for channel in (await tg_store.channels(db)).values():
        if str(channel.get("username") or "").lstrip("@").lower() == name:
            title = channel.get("title")
            return str(title) if isinstance(title, str) and title.strip() else None
    return None


async def overview(engine: AsyncEngine, *, wanted: str) -> dict[str, Any]:
    """The «فهرست ‎@MTProxybot‎» section of GET /api/sponsor. No secret: addresses, label shapes,
    states, the public sponsor channel each live proxy's screen names, and screen shapes."""
    async with engine.connect() as db:
        state = await load_state(db)
        _, nodes, entries, mapped = await _context(db)
        wanted_title = await _channel_title(db, wanted)
    views = [_entry_view(e, m, nodes, wanted, wanted_title) for e, m in zip(entries, mapped, strict=True)]
    counts: dict[str, int] = {"live": 0, "gone": 0, "retired": 0, "unknown": 0}
    for view in views:
        counts[view["status"]] = counts.get(view["status"], 0) + 1
    counts["removable"] = sum(1 for v in views if v["removable"])
    counts["promotion_ok"] = sum(1 for v in views if v["promotion"] == "ok")
    counts["promotion_problem"] = sum(1 for v in views if v["promotion"] in ("other", "not_shown", "several"))
    counts["promotion_unread"] = sum(1 for v in views if v["promotion"] in ("unread", "stuck"))
    return {
        "removal_enabled": state.removal_enabled,
        "cleanup_mode": REMOVE if state.removal_enabled else OBSERVE,
        "wanted_channel": wanted,
        "job": _job_view(state.job),
        "list": state.last_list.to_json() if state.last_list is not None else None,
        "checked_at": iso(state.checked_at),
        "observation": state.observation,
        "counts": counts,
        "entries": views,
        "screens": list(state.screens),
    }


async def status(engine: AsyncEngine, *, wanted: str) -> dict[str, Any]:
    """What the CLI prints (the operator's report): the overview without the removal hint texts."""
    view = await overview(engine, wanted=wanted)
    view["gone_reasons"] = sorted(botlist.GONE_REASONS)
    view["statuses"] = [LIVE, GONE, botlist.RETIRED_ALIVE, botlist.UNKNOWN]
    return view
