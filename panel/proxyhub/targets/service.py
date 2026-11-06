"""Candidate sites from Iran: measurement rounds (network I/O), their storage, our nodes' own checks,
the ``auto`` choice applied to the Reality and MTProto settings, and the dashboard summary.

A round: one TLS control (Globalping ``http``, HTTPS ``HEAD /`` of the probes' TLS control host
from MCI and Rightel), then each site on 443 reusing exactly the control's probes. A site counts on
a network only where the control worked from that network. Results go into the probe tables (role
``target``) and, per site, into ``target_states`` (latest two rounds). Full rounds run every
``interval_h`` hours; a current target that looked closed is measured again after
:data:`CONFIRM_AFTER` (two rounds decide), and a site the owner just added is measured within
minutes. Globalping's hourly allowance is shared: a round runs only when the remaining tests cover it
plus a full node-probe cycle.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_net import ProbeCycle, TargetState
from proxyhub.fleet.actions import iso, log_action
from proxyhub.mtproto.settings import (
    MtprotoSettings,
    SettingsCorrupt,
    load_settings,
    load_settings_for_update,
    save_settings,
    settings_json,
)
from proxyhub.probes.globalping import GlobalpingClient, RateLimit, https_body, location_asns
from proxyhub.probes.service import store_measurement
from proxyhub.probes.settings import ProbeSettings
from proxyhub.probes.types import IRAN_NETWORKS_FA, Measurement
from proxyhub.reality.settings import SETTINGS_KEY as REALITY_KEY
from proxyhub.reality.settings import RealitySettings, load_reality_settings, save_reality_settings
from proxyhub.reality.store import all_statuses
from proxyhub.targets.choice import (
    Candidate,
    Choice,
    NetResult,
    choose_mtproto,
    choose_reality,
    confirm_due,
    iran_view,
    node_check,
    open_from_iran,
)
from proxyhub.targets.settings import TargetSettings, load_state, save_state
from proxyhub.telegram.store import enqueue_owner_alert, get_setting

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
ACTOR: Final = "targets"
PORT: Final = 443
GP_CONCURRENCY: Final = 4
CONFIRM_AFTER: Final = timedelta(minutes=30)
RETRY_AFTER: Final = timedelta(minutes=30)  # after a round that could not run (budget, no probes)
NEW_HOST_GAP: Final = timedelta(minutes=10)
NODE_REPORT_FRESH: Final = timedelta(hours=1)
NO_CANDIDATE_REPEAT: Final = timedelta(days=1)


# --- what to measure ------------------------------------------------------------------------------


def target_locations(probes: ProbeSettings) -> list[dict[str, Any]]:
    """MCI and Rightel (the probes' verdict networks) with the probe limits of the TLS locations."""
    limits = {loc.asn: loc.limit for loc in probes.tls_locations if loc.asn is not None}
    return [{"asn": a, "limit": limits.get(a, 2)} for a in probes.verdict_asns]


def round_cost(hosts: Sequence[str], probes: int, control_host: str) -> int:
    """Globalping tests of one round: the control plus every site (the control host's own result
    is the control's)."""
    return probes * (1 + sum(1 for h in hosts if h != control_host))


def budget_allows(rate: RateLimit | None, now: datetime, cost: int, reserve: int) -> bool:
    if rate is None or rate.reset_at <= now:
        return True
    return rate.remaining >= cost + reserve


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Due:
    hosts: tuple[str, ...]
    full: bool
    reason: str  # 'schedule' | 'confirm' | 'new'


def due_round(
    state: Mapping[str, Any],
    settings: TargetSettings,
    hosts: Sequence[str],
    current: Sequence[str],
    states: Mapping[str, Candidate],
    keys: Sequence[str],
    now: datetime,
) -> Due | None:
    """The next round, if one is due (pure)."""
    last_full = _parse_at(state.get("last_full_at"))
    last_try = _parse_at(state.get("last_attempt_at"))
    if last_try is not None and now - last_try < NEW_HOST_GAP:
        return None  # at most one attempt every few minutes, whatever happened to the last one
    full_due = last_full is None or now - last_full >= timedelta(hours=settings.interval_h)
    retry_ok = last_try is None or last_full is None or last_try <= last_full or now - last_try >= RETRY_AFTER
    if full_due and retry_ok:
        return Due(tuple(hosts), True, "schedule")
    confirm = [h for h in current if confirm_due(states.get(h), keys, now, CONFIRM_AFTER)]
    if confirm and (last_try is None or now - last_try >= CONFIRM_AFTER):
        return Due(tuple(dict.fromkeys(confirm)), False, "confirm")
    new = [h for h in hosts if h not in states]
    if new and (last_try is None or now - last_try >= NEW_HOST_GAP):
        return Due(tuple(new), False, "new")
    return None


# --- one round (network I/O only) ---------------------------------------------------------------


@dataclass(frozen=True)
class TargetRound:
    started_at: datetime
    finished_at: datetime
    control: Measurement
    results: Mapping[str, Measurement]
    rate: RateLimit | None


async def measure_round(
    gp: GlobalpingClient, hosts: Sequence[str], control_host: str, locations: Sequence[Mapping[str, Any]]
) -> TargetRound:
    started = datetime.now(UTC)
    asns = location_asns(locations)
    control, control_id = await gp.run(
        https_body(control_host, PORT, locations),
        kind="https",
        role="control",
        target=control_host,
        port=PORT,
        requested_asns=asns,
    )
    where: str | Sequence[Mapping[str, Any]] | None
    if control_id is not None:
        where = control_id
    elif control.status in ("error", "timeout"):
        where = locations
    else:
        where = None
    sem = asyncio.Semaphore(GP_CONCURRENCY)

    async def one(host: str) -> tuple[str, Measurement]:
        if host == control_host and control.status == "ok":
            return host, replace(control, role="target")  # the very same probes, the same moment
        if where is None:
            return host, Measurement(
                "globalping", "https", "target", host, PORT, control.status, datetime.now(UTC)
            )
        async with sem:
            m, _ = await gp.run(
                https_body(host, PORT, where),
                kind="https",
                role="target",
                target=host,
                port=PORT,
                requested_asns=asns,
            )
        return host, m

    results = dict(await asyncio.gather(*(one(h) for h in hosts)))
    return TargetRound(started, datetime.now(UTC), control, results, gp.last_rate)


# --- storage ----------------------------------------------------------------------------------------


def net_results(m: Measurement, control: Measurement) -> dict[str, NetResult]:
    """A site's per-network result: valid only where the round's control worked from the same
    network (else ``nodata``)."""
    control_up = (
        {n.network_key for n in control.networks if n.state == "up"} if control.status == "ok" else set()
    )
    out: dict[str, NetResult] = {}
    for n in m.networks:
        if m.status != "ok" or n.network_key not in control_up or n.state == "nodata":
            state: Any = "nodata"
        else:
            state = n.state
        ms = n.detail.get("total_ms") if state == "up" else None
        out[n.network_key] = NetResult(
            state, n.network, n.asn, float(ms) if isinstance(ms, int | float) else None
        )
    return out


async def store_round(db: Db, rnd: TargetRound) -> list[str]:
    """Store the measurements; shift each site's latest round to ``iran_prev``. A site whose round
    gave nothing valid keeps its earlier rounds. Returns the sites updated."""
    cycle_id = (
        await db.execute(
            insert(ProbeCycle)
            .values(started_at=rnd.started_at, finished_at=rnd.finished_at)
            .returning(ProbeCycle.id)
        )
    ).scalar_one()
    await store_measurement(db, cycle_id, rnd.control)
    updated: list[str] = []
    table = TargetState.__table__
    for host, m in sorted(rnd.results.items()):
        await store_measurement(db, cycle_id, m)
        nets = net_results(m, rnd.control)
        iran = {k: r.to_json() for k, r in sorted(nets.items())}
        if not any(r.state != "nodata" for r in nets.values()):
            # Nothing valid: a site seen for the first time gets an empty row (so it is not "new"
            # again every few minutes); a known site keeps its earlier rounds.
            empty = insert(TargetState).values(host=host, iran={}, iran_prev={})
            await db.execute(empty.on_conflict_do_nothing(index_elements=[TargetState.host]))
            continue
        stmt = insert(TargetState).values(host=host, measured_at=rnd.started_at, iran=iran, iran_prev={})
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[TargetState.host],
                set_={
                    "prev_measured_at": table.c.measured_at,
                    "iran_prev": table.c.iran,
                    "measured_at": stmt.excluded.measured_at,
                    "iran": stmt.excluded.iran,
                    "updated_at": rnd.finished_at,
                },
            )
        )
        updated.append(host)
    return updated


async def load_candidates(db: Db) -> dict[str, Candidate]:
    rows = (await db.execute(select(TargetState.__table__))).all()
    return {
        r.host: Candidate(
            host=r.host,
            measured_at=r.measured_at,
            iran={str(k): NetResult.from_json(v) for k, v in dict(r.iran or {}).items()},
            prev_measured_at=r.prev_measured_at,
            iran_prev={str(k): NetResult.from_json(v) for k, v in dict(r.iran_prev or {}).items()},
            node_ok_at=r.node_ok_at,
            node_fail_at=r.node_fail_at,
        )
        for r in rows
    }


async def record_node_checks(db: Db, hosts: Sequence[str], now: datetime) -> dict[str, str]:
    """Our nodes' own TLS 1.3 + HTTP/2 checks of a REALITY target (``target_ok``, reported with the
    target it was about): a site passes when at least one node that reported within the last hour
    passed it, and fails when every such node failed it. Returns host -> 'passed' / 'failed'."""
    wanted = set(hosts)
    passes: dict[str, int] = {}
    fails: dict[str, int] = {}
    for s in (await all_statuses(db)).values():
        name = s.checked_server_name
        if s.target_ok is None or name is None or name not in wanted:
            continue
        if s.checked_target != f"{name}:{PORT}" or now - s.reported_at > NODE_REPORT_FRESH:
            continue
        (passes if s.target_ok else fails)[name] = (passes if s.target_ok else fails).get(name, 0) + 1
    out: dict[str, str] = {}
    for host in sorted(set(passes) | set(fails)):
        passed = passes.get(host, 0) > 0
        column = "node_ok_at" if passed else "node_fail_at"
        stmt = insert(TargetState).values(host=host, iran={}, iran_prev={}, **{column: now})
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[TargetState.host], set_={column: now, "updated_at": now}
            )
        )
        out[host] = "passed" if passed else "failed"
    return out


# --- the auto choice -------------------------------------------------------------------------------


def _nets_fa(keys: Sequence[str]) -> str:
    names = [IRAN_NETWORKS_FA.get(int(k.split(":", 1)[1]), k) if k.startswith("asn:") else k for k in keys]
    return " و ".join(names)


def why_fa(choice: Choice) -> str:
    if choice.why == "risky_sni":
        return (
            "نامش از نام‌هایی است که خود Xray درباره‌شان هشدار میدهد (apple، icloud، microsoft، یا "
            "‎.ru، ‎.ir و ‎.cn): چنین SNIای احتمال بسته شدن IP سرور را بالا میبرد"
        )
    if choice.why == "not_a_candidate":
        return "در فهرست سایت‌های نامزد (صفحه‌ی «تنظیمات») نیست"
    if choice.why == "node_check_failed":
        return "آزمون TLS 1.3 و HTTP/2 خود nodeها را رد کرد"
    return f"در دو دور پشت‌سرهم از {_nets_fa(choice.networks)} با دست‌دادن TLS بسته بود"


def reality_switch_text(old: str, new: str, choice: Choice, transport: str, node_passed: bool) -> str:
    checked = (
        " و nodeهای ما هم آزمون TLS 1.3 و HTTP/2 آن را قبول کرده‌اند"
        if node_passed
        else "؛ nodeها آن را خودشان میآزمایند و کانفیگ تازه فقط پس از قبول شدن ساخته میشود"
    )
    if choice.why != "risky_sni":
        old_configs = (
            "کانفیگ‌های فعالِ هدف قبلی تا پایان زمان خودشان کار میکنند (مگر هدف قبلی از خود node رد شده باشد). "
        )
    elif transport == "reality":
        # proxyhub/reality/policy.py retarget_decision: a scope leaves a risky target at once.
        old_configs = (
            "کانفیگ‌های فعالِ هدف قبلی همین حالا کنار میروند (🚫 روی پست‌ها) و کانفیگ تازه با هدف "
            "تازه ساخته میشود. "
        )
    else:
        old_configs = ""
    text = (
        f"🔁 <b>Reality</b>: سایت هدف خودکار از <code>{html.escape(old)}</code> به "
        f"<code>{html.escape(new)}</code> عوض شد، چون هدف قبلی {why_fa(choice)}. "
        f"<code>{html.escape(new)}</code> از ایران (همراه اول و رایتل) با دست‌دادن TLS باز است{checked}.\n"
        f"{old_configs}برای انتخاب دستی، در صفحه‌ی Reality حالت «دستی» را بزنید."
    )
    if transport != "reality":
        text += "\nکانفیگ‌های فعلی WebSocket + TLS اند؛ این هدف وقتی به کار میآید که Reality روشن شود."
    return text


def mtproto_switch_text(old: str, new: str, choice: Choice) -> str:
    return (
        f"🔁 <b>MTProto</b>: دامنه‌ی TLS خودکار از <code>{html.escape(old)}</code> به "
        f"<code>{html.escape(new)}</code> عوض شد، چون دامنه‌ی قبلی {why_fa(choice)}. "
        "این فقط برای nodeهایی است که دامنه‌ی خودشان را ندارند؛ لینک پروکسی آن‌ها با دامنه‌ی تازه "
        "ساخته میشود. nodeهای دارای دامنه‌ی خودشان همان نام خودشان را نگه میدارند (D42). "
        "برای انتخاب دستی، در صفحه‌ی MTProto حالت «دستی» را بزنید."
    )


def no_candidate_text(kinds: Sequence[str]) -> str:
    what = " و ".join("سایت هدف Reality" if k == "reality" else "دامنه‌ی TLS پروکسی MTProto" for k in kinds)
    return (
        f"⚠️ {what} از ایران مشکل دارد، ولی هیچ‌کدام از سایت‌های نامزد در «تنظیمات» از همراه اول و رایتل "
        "باز نبود؛ فعلاً همان قبلی میماند. چند سایت بزرگ دیگر (TLS 1.3 و HTTP/2) را به فهرست نامزدها "
        "اضافه کنید؛ هر سایت تازه چند دقیقه بعد از ایران سنجیده میشود."
    )


@dataclass(frozen=True)
class Switched:
    kind: str  # 'reality' | 'mtproto'
    old: str
    new: str
    why: str | None


async def apply_auto(db: Db, settings: TargetSettings, keys: Sequence[str], now: datetime) -> list[Switched]:
    """Apply the ``auto`` choices to the Reality and MTProto settings (in the caller's transaction),
    log every switch and tell the owner. Nothing changes in ``manual`` mode."""
    states = await load_candidates(db)
    listed = settings.candidates
    out: list[Switched] = []
    stuck: list[str] = []

    reality = await load_reality_settings(db)
    if reality.target_mode == "auto" and not await _reality_corrupt(db):
        choice = choose_reality(reality.target_host, listed, states, keys, now)
        if choice.switch:
            new = RealitySettings.model_validate(
                {**reality.model_dump(mode="json"), "target_host": choice.host, "target_port": PORT}
            )
            await save_reality_settings(db, new)
            out.append(Switched("reality", reality.target_host, choice.host, choice.why))
            passed = node_check(states.get(choice.host), now) == "passed"
            await _logged_switch(db, "reality", reality.target_host, choice)
            await enqueue_owner_alert(
                db, reality_switch_text(reality.target_host, choice.host, choice, reality.transport, passed)
            )
        elif choice.why is not None:
            stuck.append("reality")

    try:
        mt: MtprotoSettings | None = await load_settings(db)
    except SettingsCorrupt:
        mt = None  # the owner repairs it on the MTProto page; nothing is chosen meanwhile
    if mt is not None and mt.target_mode == "auto":
        choice = choose_mtproto(mt.tls_domain, listed, states, keys, now)
        if choice.switch:
            # Lock the row and look again: an owner's save in between wins.
            locked = await load_settings_for_update(db)
            if locked is None or locked != mt:
                return out
            await save_settings(
                db, MtprotoSettings.model_validate({**settings_json(mt), "tls_domain": choice.host})
            )
            out.append(Switched("mtproto", mt.tls_domain, choice.host, choice.why))
            await _logged_switch(db, "mtproto", mt.tls_domain, choice)
            await enqueue_owner_alert(db, mtproto_switch_text(mt.tls_domain, choice.host, choice))
        elif choice.why is not None:
            stuck.append("mtproto")

    # "No open candidate" is news only once the candidates have been measured (not on a fresh panel).
    measured = any(iran_view(states.get(h), keys, now).fresh for h in listed)
    if stuck and measured:
        state = await load_state(db)
        last = _parse_at(state.get("no_candidate_alert_at"))
        if last is None or now - last >= NO_CANDIDATE_REPEAT:
            await enqueue_owner_alert(db, no_candidate_text(stuck))
            await log_action(
                db,
                actor=ACTOR,
                action="target_no_candidate",
                target=",".join(stuck),
                dry_run=False,
                allowed=False,
                reasons=["no_open_candidate"],
                context={"candidates": list(listed)},
                result="the current target stays: no candidate is open from Iran",
            )
            state["no_candidate_alert_at"] = now.isoformat()
            await save_state(db, state)
    return out


async def _reality_corrupt(db: Db) -> bool:
    """Stored Reality settings that no longer validate: the loop then works on the defaults, and
    saving a switch would silently replace the owner's values with them. Leave it to the owner."""
    raw = await get_setting(db, REALITY_KEY)
    if raw is None:
        return False
    try:
        RealitySettings.model_validate(raw)
    except ValidationError:
        return True
    return False


async def _logged_switch(db: Db, kind: str, old: str, choice: Choice) -> None:
    await log_action(
        db,
        actor=ACTOR,
        action="target_auto_switched",
        target=kind,
        dry_run=False,
        allowed=True,
        reasons=[choice.why or "auto"],
        context={"from": old, "to": choice.host, "why": choice.why, "networks": list(choice.networks)},
        result=(
            f"{kind} target {old} -> {choice.host} (auto): "
            + (
                "active configs of the old target end at their own time unless it fails on the nodes"
                if kind == "reality"
                else "only nodes without their own DNS name use it (D42)"
            )
        ),
    )


# --- the dashboard -----------------------------------------------------------------------------


def _nets_json(nets: Mapping[str, NetResult], keys: Sequence[str]) -> list[dict[str, Any]]:
    order = sorted(nets, key=lambda k: (keys.index(k) if k in keys else len(keys), nets[k].network))
    return [{"key": k, **nets[k].to_json(), "key_network": k in keys} for k in order]


async def summary(db: Db, settings: TargetSettings, keys: Sequence[str], now: datetime) -> dict[str, Any]:
    states = await load_candidates(db)
    reality = await load_reality_settings(db)
    try:
        mt: MtprotoSettings | None = await load_settings(db)
    except SettingsCorrupt:
        mt = None
    current: dict[str, list[str]] = {}
    current.setdefault(reality.target_host, []).append("reality")
    if mt is not None:
        current.setdefault(mt.tls_domain, []).append("mtproto")
    hosts = list(dict.fromkeys([*settings.candidates, *current]))
    state = await load_state(db)
    items: list[dict[str, Any]] = []
    for host in hosts:
        c = states.get(host)
        v = iran_view(c, keys, now)
        items.append(
            {
                "host": host,
                "listed": host in settings.candidates,
                "current_for": current.get(host, []),
                "measured_at": iso(c.measured_at) if c else None,
                "networks": _nets_json(c.iran, keys) if c else [],
                "prev_measured_at": iso(c.prev_measured_at) if c else None,
                "prev_networks": _nets_json(c.iran_prev, keys) if c else [],
                "open": open_from_iran(c, keys, now),
                "blocked_on": list(v.blocked_on),
                "ms": v.ms,
                "node_check": node_check(c, now),
                "node_ok_at": iso(c.node_ok_at) if c else None,
                "node_fail_at": iso(c.node_fail_at) if c else None,
            }
        )
    last_full = _parse_at(state.get("last_full_at"))
    return {
        "settings": settings.model_dump(mode="json"),
        "defaults": TargetSettings().model_dump(mode="json"),
        "key_networks": list(keys),
        "reality": {
            "mode": reality.target_mode,
            "target": reality.target_host,
            "transport": reality.transport,
        },
        "mtproto": {"mode": mt.target_mode, "tls_domain": mt.tls_domain} if mt is not None else None,
        "last_full_at": iso(last_full),
        "next_full_at": iso(last_full + timedelta(hours=settings.interval_h)) if last_full else None,
        "candidates": items,
    }
