"""Test nodes for the API and the CLI: request one, retire it, list nodes, read a node's events.

Creation is decided by :func:`evaluate_create`, which feeds ``proxyhub.safety.gates.can_create``
with the owner's caps, the live month projection (live Hetzner prices, never a guess) and the
account's last live observation. The API answers from stored observations; the executor evaluates
again with a fresh live look at the account right before it creates anything.

Retirement is decided by ``can_delete`` on a LIVE read of the server (label ``managed-by``, role,
our DB row with the same Hetzner id and account, delete protection), as the executor will see it.

Nothing secret passes through here: no token is read except the account's Hetzner token for the
live read (revealed only into the Hetzner client), and nothing is returned but ids, states and
reason codes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.accounts.hetzner_io import HetznerFailure, LiveOffer, call_hetzner
from proxyhub.db.models import Node, NodeEvent
from proxyhub.db.models_agent import AgentNode, TelemtUser
from proxyhub.db.models_live import NodeOperation
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.costs import money, node_cost_items
from proxyhub.fleet.lifecycle import LIVE_STATES, NodeState
from proxyhub.fleet.planner import CREATION_WINDOW, OBSERVATION_MAX_AGE, OFFER_MAX_AGE, AccountSnapshot
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.fleet.snapshot import load_accounts, load_nodes, load_offers
from proxyhub.hetzner.client import HetznerReader, _delete_protected
from proxyhub.nodes.settings import get_live_settings
from proxyhub.nodes.transitions import event, lock_node, transition
from proxyhub.reconcilers.base import lock_key
from proxyhub.safety.budget import projected_month_total, rest_of_month_cost
from proxyhub.safety.gates import (
    CREATE_ACCOUNT_STATES,
    AccountView,
    CreateRequest,
    NodeRowView,
    OperatorLimits,
    ServerView,
    can_create,
    can_delete,
)
from proxyhub.settings_schema import FleetSettings
from proxyhub.vault import Vault, VaultError, hcloud_secret_name

TEST_NODE_PREFIX = "ph-test-"
# Nodes deleted longer ago than this are no longer listed by GET /api/nodes.
LIST_DELETED_FOR = timedelta(days=7)
EVENTS_LIMIT = 200
_TEST_NODE_LOCK = "nodes:test_node_request"

MESSAGES_FA: dict[str, str] = {
    "gate_refused": "قفل‌های ایمنی این کار را رد کردند. دلیل‌ها در فهرست آمده‌اند.",
    "test_node_exists": "یک node آزمایشی زنده هست. اول همان را بازنشسته کنید.",
    "not_found": "این node پیدا نشد.",
}

# Persian text of every reason code a node request or retirement can be refused with (the API adds
# them as "reasons_fa", in the same order as "reasons"; an unknown code is shown as it is).
REASONS_FA: dict[str, str] = {
    # proxyhub.safety.gates.can_create
    "kill_switch_on": "کلید توقف روشن است.",
    "budget_cap_not_set": "سقف بودجه‌ی ماهانه تعیین نشده است.",
    "node_cap_not_set": "سقف تعداد node تعیین نشده است.",
    "node_cap_reached": "تعداد nodeها به سقف رسیده است.",
    "budget_cap_reached": "با این سرور، خرج پیش‌بینی‌شده‌ی ماه از سقف بودجه بیشتر می‌شود.",
    "creation_rate_cap_reached": "سقف تعداد ساخت در یک ساعت پر شده است.",
    "account_not_usable": "حساب هتزنر در وضعیتی نیست که در آن سرور ساخته شود.",
    "account_guard_failed": (
        "نگهبان حساب سبز نیست؛ در project سروری هست که مال این سیستم نیست یا وارسی ناموفق بوده است."
    ),
    "account_server_limit_reached": "حساب هتزنر به سقف تعداد سرورش رسیده است.",
    "invalid_cost_input": "محاسبه‌ی هزینه نامعتبر بود.",
    # proxyhub.nodes
    "settings_invalid": "تنظیمات ذخیره‌شده‌ی ناوگان خراب است.",
    "agent_domain_not_set": "دامنه‌ی سایت عامل‌ها (AGENT_DOMAIN) تنظیم نشده است.",
    "projection_incomplete": "قیمت زنده‌ی بعضی nodeها معلوم نیست؛ خرج ماه کامل محاسبه نمی‌شود.",
    "prices_stale": "قیمت‌های هتزنر بیش از ۶ ساعت است تازه نشده‌اند.",
    "location_not_allowed": "این location در فهرست locationهای مجاز تنظیمات نیست.",
    "no_location_configured": "هیچ location مجازی در تنظیمات نیست.",
    "no_usable_account": "هیچ حساب هتزنر فعالی برای ساخت نیست.",
    "no_live_price": "برای نوع سرور مجاز در این location قیمت زنده یا موجودی نیست.",
    "account_observation_stale": "آخرین وارسی زنده‌ی حساب هتزنر بیش از ۳۰ دقیقه پیش بوده است.",
    "test_node_exists": "یک node آزمایشی زنده هست.",
    "token_missing": "توکن این حساب هتزنر در گاوصندوق نیست.",
    "user_data_failed": "ساختن cloud-init برای این node ناموفق بود.",
    "vault_unavailable": "کلید رمزنگاری پنل در دسترس نیست.",
    "node_changed_meanwhile": "وضعیت node هم‌زمان عوض شد؛ دوباره امتحان کنید.",
    "node_deleted": "این node پیش‌تر حذف شده است.",
    "fleet_not_live": "ناوگان ۹ جایگاهی هنوز فقط حالت خشک دارد؛ فقط node آزمایشی حذف‌شدنی است.",
    "hetzner_unreachable": "API هتزنر پاسخ نداد.",
    "hetzner_rate_limited": "سقف درخواست API هتزنر پر شده است.",
    "hetzner_token_invalid": "هتزنر توکن این حساب را نپذیرفت.",
    "hetzner_forbidden": "هتزنر دسترسی این توکن را رد کرد.",
    "hetzner_read_only": "توکن این حساب فقط‌خواندنی است.",
    "hetzner_api_error": "API هتزنر خطا داد.",
    # proxyhub.safety.gates.can_delete (on a live read of the server)
    "label_managed_by_missing": "برچسب managed-by=proxy-hub روی سرور نیست.",
    "role_not_deletable": "نقش سرور (برچسب role) حذف‌شدنی نیست.",
    "no_db_row": "این سرور در پایگاه‌داده‌ی پنل ثبت نیست.",
    "db_row_hetzner_id_mismatch": "شناسه‌ی هتزنر سرور با ردیف پایگاه‌داده یکی نیست.",
    "db_row_account_mismatch": "حساب سرور با ردیف پایگاه‌داده یکی نیست.",
    "db_row_not_retiring": "ردیف پایگاه‌داده در وضعیت بازنشستگی نیست.",
    "delete_protection_on": "حفاظت از حذف روی سرور روشن است.",
}


def reasons_fa(reasons: list[str] | tuple[str, ...]) -> list[str]:
    return [REASONS_FA.get(r, r) for r in reasons]


class NodeServiceError(Exception):
    code = "invalid_request"


class GateRefused(NodeServiceError):
    code = "gate_refused"

    def __init__(self, reasons: list[str] | tuple[str, ...]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = list(reasons)


class TestNodeExists(NodeServiceError):
    code = "test_node_exists"


class NodeNotFound(NodeServiceError):
    code = "not_found"


# --- creation gates --------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveAccountLook:
    """A look at ALL servers of the account taken just now (executor only)."""

    guard_ok: bool
    servers_total: int


@dataclass(frozen=True)
class CreateEvaluation:
    allowed: bool
    reasons: tuple[str, ...]
    limits: OperatorLimits
    request: CreateRequest
    account_id: uuid.UUID | None
    account_name: str | None
    location: str | None
    server_type: str | None
    context: dict[str, Any] = field(default_factory=dict)


def _cheapest(offers: list[LiveOffer], allowed_types: list[str], server_type: str | None) -> LiveOffer | None:
    # A node keeps the type it was requested with, but only while the owner still allows that type.
    wanted = [t for t in allowed_types if server_type is None or t == server_type]
    usable = [o for o in offers if o.server_type in wanted and o.orderable]
    usable.sort(key=lambda o: (o.price.monthly, o.price.hourly, o.server_type))
    return usable[0] if usable else None


async def load_settings(db: AsyncSession) -> tuple[FleetSettings, list[str]]:
    try:
        return await load_fleet_settings(db), []
    except SettingsCorrupt:
        return FleetSettings(), ["settings_invalid"]  # defaults: kill switch on, no caps


async def evaluate_create(
    db: AsyncSession,
    now: datetime,
    *,
    location: str | None = None,
    account_id: uuid.UUID | None = None,
    server_type: str | None = None,
    exclude_node: uuid.UUID | None = None,
    live: LiveAccountLook | None = None,
) -> CreateEvaluation:
    """Would ``can_create`` allow one more (test) server now? Reasons list every refusal.

    ``exclude_node`` leaves the node being evaluated out of every count (the executor evaluates a
    node that already has its ``requested`` row). ``live`` replaces the stored account observation
    with a look taken just now.
    """
    settings, reasons = await load_settings(db)
    limits = settings.limits()
    if get_live_settings().checked_agent_domain() is None:
        reasons.append("agent_domain_not_set")

    nodes = [n for n in await load_nodes(db, now) if exclude_node is None or n.id != str(exclude_node)]
    stored_offers = await load_offers(db)
    prices = {
        (ao.account_id, ao.offer.server_type, ao.offer.location): ao.offer.price for ao in stored_offers
    }
    items, missing = node_cost_items(nodes, prices, now)
    if missing:
        reasons.append("projection_incomplete")
    if any(ao.fetched_at is not None and now - ao.fetched_at > OFFER_MAX_AGE for ao in stored_offers):
        reasons.append("prices_stale")
    offers_by_account: dict[str, list[LiveOffer]] = {}
    for ao in stored_offers:
        offers_by_account.setdefault(ao.account_id, []).append(ao.offer)

    if location is not None and location not in settings.locations:
        reasons.append("location_not_allowed")
    locations = [location] if location is not None else list(settings.locations)
    if not locations:
        reasons.append("no_location_configured")

    accounts = await load_accounts(db)
    if account_id is not None:
        candidates = [a for a in accounts if a.id == str(account_id)]
    else:
        candidates = sorted(
            (a for a in accounts if a.state in CREATE_ACCOUNT_STATES),
            key=lambda a: a.created_at,
            reverse=True,
        )

    def fresh(a: AccountSnapshot) -> bool:
        return a.observed_at is not None and now - a.observed_at <= OBSERVATION_MAX_AGE

    chosen: AccountSnapshot | None = None
    chosen_loc: str | None = locations[0] if locations else None
    chosen_offer: LiveOffer | None = None
    fallback: tuple[AccountSnapshot, str, LiveOffer] | None = None
    for a in candidates:
        for loc in locations:
            offer = _cheapest(
                [o for o in offers_by_account.get(a.id, []) if o.location == loc],
                settings.allowed_server_types,
                server_type,
            )
            if offer is None:
                continue
            good = live.guard_ok if live is not None else (fresh(a) and a.guard_ok is True)
            if good:
                chosen, chosen_loc, chosen_offer = a, loc, offer
                break
            if fallback is None:
                fallback = (a, loc, offer)
        if chosen is not None:
            break
    if chosen is None and fallback is not None:
        chosen, chosen_loc, chosen_offer = fallback
    if chosen is None and candidates:
        chosen = candidates[0]
    if chosen is None:
        reasons.append("no_usable_account")
    elif chosen_offer is None:
        reasons.append("no_live_price")
    if chosen is not None and live is None and not fresh(chosen):
        reasons.append("account_observation_stale")

    live_nodes = [n for n in nodes if n.state in LIVE_STATES]
    in_account = sum(1 for n in live_nodes if chosen is not None and n.account_id == chosen.id)
    if chosen is not None:
        if live is not None:
            guard_ok, servers = live.guard_ok, max(live.servers_total, in_account)
        else:
            guard_ok, servers = (
                fresh(chosen) and chosen.guard_ok is True,
                max(chosen.servers_total or 0, in_account),
            )
        view = AccountView(
            id=chosen.id,
            state=chosen.state,
            guard_ok=guard_ok,
            learned_server_limit=chosen.learned_server_limit,
            servers_in_account=servers,
        )
    else:
        view = AccountView(
            id="-", state="none", guard_ok=False, learned_server_limit=None, servers_in_account=0
        )
    cost = rest_of_month_cost(chosen_offer.price, now) if chosen_offer is not None else Decimal(0)
    projected = projected_month_total(items, now)
    request = CreateRequest(
        account=view,
        fleet_live_total=len(live_nodes),
        creations_last_hour=sum(1 for n in nodes if n.created_at > now - CREATION_WINDOW),
        projected_month_eur=projected,
        new_server_rest_of_month_eur=cost,
    )
    decision = can_create(limits, request)
    reasons.extend(r for r in decision.reasons if r not in reasons)
    context: dict[str, Any] = {
        "account_id": chosen.id if chosen else None,
        "account_name": chosen.name if chosen else None,
        "location": chosen_loc,
        "server_type": chosen_offer.server_type if chosen_offer else None,
        "price_monthly_eur": money(chosen_offer.price.monthly) if chosen_offer else None,
        "rest_of_month_eur": money(cost),
        "projected_month_eur": money(projected),
        "budget_cap_eur": str(limits.budget_cap_eur) if limits.budget_cap_eur is not None else None,
        "node_cap_total": limits.node_cap_total,
        "fleet_live_total": request.fleet_live_total,
        "creations_last_hour": request.creations_last_hour,
        "servers_in_account": view.servers_in_account,
        "live_look": live is not None,
    }
    return CreateEvaluation(
        allowed=decision.allowed and not reasons,
        reasons=tuple(reasons),
        limits=limits,
        request=request,
        account_id=uuid.UUID(chosen.id) if chosen else None,
        account_name=chosen.name if chosen else None,
        location=chosen_loc,
        server_type=chosen_offer.server_type if chosen_offer else None,
        context=context,
    )


async def other_live_test_node(db: AsyncSession, exclude: uuid.UUID | None = None) -> uuid.UUID | None:
    stmt = select(Node.id).where(Node.is_test.is_(True), Node.state != NodeState.DELETED.value)
    if exclude is not None:
        stmt = stmt.where(Node.id != exclude)
    return (await db.execute(stmt.order_by(Node.created_at).limit(1))).scalar_one_or_none()


async def request_test_node(
    db: AsyncSession, *, actor: str, now: datetime, location: str | None = None
) -> uuid.UUID:
    """Record a ``requested`` test node if every gate allows it. The executor does the rest.

    At most one live test node: requests are serialized by a transaction advisory lock, and the
    executor checks again before it creates a server.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key(_TEST_NODE_LOCK)})
    if await other_live_test_node(db) is not None:
        await db.rollback()
        raise TestNodeExists()
    ev = await evaluate_create(db, now, location=location)
    if not ev.allowed or ev.account_id is None or ev.location is None or ev.server_type is None:
        await log_action(
            db,
            actor=actor,
            action="test_node_request",
            target=None,
            dry_run=False,
            allowed=False,
            reasons=ev.reasons,
            context=ev.context,
            result="refused by the safety gates; nothing was created",
        )
        await db.commit()
        raise GateRefused(ev.reasons or ("no_usable_account",))
    node_id = uuid.uuid4()
    name = f"{TEST_NODE_PREFIX}{node_id.hex[:8]}"
    db.add(
        Node(
            id=node_id,
            account_id=ev.account_id,
            name=name,
            slot=None,
            location=ev.location,
            server_type=ev.server_type,
            state=NodeState.REQUESTED.value,
            is_test=True,
            created_at=now,
            updated_at=now,
        )
    )
    await db.flush()
    db.add(NodeOperation(node_id=node_id, requested_by=actor, requested_at=now, state_since=now))
    event(db, node_id, now, "requested", by=actor, **{k: ev.context[k] for k in _REQUEST_KEYS})
    await log_action(
        db,
        actor=actor,
        action="test_node_request",
        target=name,
        dry_run=False,
        allowed=True,
        reasons=[],
        context={"node_id": str(node_id), **ev.context},
        result="test node requested; the executor creates it through the safety gates",
    )
    await db.commit()
    return node_id


_REQUEST_KEYS = ("account_name", "location", "server_type", "price_monthly_eur", "projected_month_eur")


# --- retirement --------------------------------------------------------------------------------

ReaderFactory = Callable[[str], HetznerReader]


def server_view(server: Any) -> ServerView:
    """hcloud BoundServer -> the gate's view of it (live data)."""
    return ServerView(
        hetzner_id=int(server.id or 0),
        name=str(server.name or ""),
        labels={str(k): str(v) for k, v in dict(server.labels or {}).items()},
        delete_protected=_delete_protected(server.protection),
    )


async def live_server(reader: HetznerReader, hetzner_id: int) -> ServerView | None:
    """The server as Hetzner sees it now, or None if Hetzner says it does not exist."""
    try:
        server = await call_hetzner(lambda: reader.raw.servers.get_by_id(hetzner_id))
    except HetznerFailure as failure:
        if failure.code == "not_found":
            return None
        raise
    return server_view(server)


async def account_reader(
    db: AsyncSession, vault: Vault, account_id: uuid.UUID, reader_factory: ReaderFactory
) -> HetznerReader | None:
    token = await vault.get(db, hcloud_secret_name(account_id))
    return None if token is None else reader_factory(token.reveal())


async def request_retire(
    db: AsyncSession,
    node_id: uuid.UUID,
    *,
    actor: str,
    now: datetime,
    vault_factory: Callable[[], Vault] = Vault.from_config,
    reader_factory: ReaderFactory = HetznerReader,
) -> str:
    """Ask the executor to delete a test node's server. Returns the node's new state.

    Refused (with the gate's reasons) unless the server, read live from Hetzner, would pass
    ``can_delete`` once the node is ``retiring``. A node that never reached Hetzner is closed at
    once. The executor evaluates ``can_delete`` again, live, right before it deletes.
    """
    node = await db.get(Node, node_id)
    if node is None:
        raise NodeNotFound()
    reasons: list[str] = []
    if node.state == NodeState.DELETED.value:
        reasons.append("node_deleted")
    if not node.is_test:
        reasons.append("fleet_not_live")  # the 9-slot fleet is dry-run only (FLEET_LIVE_ENABLED)
    settings, problems = await load_settings(db)
    reasons.extend(problems)
    if settings.kill_switch:
        reasons.append("kill_switch_on")
    if not reasons and node.state == NodeState.RETIRING.value:
        await db.rollback()
        return node.state  # already on its way out
    account_id, hetzner_id, seen_state, name = node.account_id, node.hetzner_id, node.state, node.name
    await db.rollback()  # no transaction (and no row lock) is held across a Hetzner call

    if not reasons and hetzner_id is not None:
        try:
            reader = await account_reader(db, vault_factory(), account_id, reader_factory)
            if reader is None:
                reasons.append("token_missing")
            else:
                server = await live_server(reader, hetzner_id)
                if server is not None:
                    row = NodeRowView(str(account_id), hetzner_id, NodeState.RETIRING.value)
                    reasons.extend(can_delete(server, row, str(account_id)).reasons)
        except HetznerFailure as failure:
            reasons.append(f"hetzner_{failure.kind}")
        except VaultError:
            reasons.append("vault_unavailable")
        await db.rollback()

    locked = await lock_node(db, node_id)
    if not reasons and (locked is None or locked.state != seen_state or locked.hetzner_id != hetzner_id):
        reasons.append("node_changed_meanwhile")
    if reasons or locked is None:
        await log_action(
            db,
            actor=actor,
            action="node_retire_request",
            target=name,
            dry_run=False,
            allowed=False,
            reasons=reasons,
            context={"node_id": str(node_id), "state": seen_state, "hetzner_id": hetzner_id},
            result="refused; nothing was changed",
        )
        await db.commit()
        raise GateRefused(reasons)

    if locked.state == NodeState.REQUESTED.value:
        target, result = NodeState.DELETED, "request withdrawn before any Hetzner call"
    else:
        target, result = NodeState.RETIRING, "retiring; the executor deletes it through the delete gate"
    await transition(db, locked, target, now, reason="owner_retire", detail={"by": actor})
    op = await db.get(NodeOperation, node_id)
    assert op is not None  # transition() created it
    if target == NodeState.RETIRING:
        op.retire_requested_at, op.retire_reason = now, "owner"
    await log_action(
        db,
        actor=actor,
        action="node_retire_request",
        target=locked.name,
        dry_run=False,
        allowed=True,
        reasons=[],
        context={"node_id": str(node_id), "from": seen_state, "hetzner_id": hetzner_id},
        result=result,
    )
    await db.commit()
    return target.value


# --- reading -----------------------------------------------------------------------------------


async def list_nodes(db: AsyncSession, now: datetime) -> list[dict[str, Any]]:
    """GET /api/nodes: every node not deleted, plus the ones deleted within the last week."""
    connections = (
        select(TelemtUser.node_id, func.sum(TelemtUser.last_current_connections).label("conns"))
        .where(TelemtUser.last_stats_at.is_not(None))
        .group_by(TelemtUser.node_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Node, AgentNode, connections.c.conns)
            .outerjoin(AgentNode, AgentNode.node_id == Node.id)
            .outerjoin(connections, connections.c.node_id == Node.id)
            .where((Node.state != NodeState.DELETED.value) | (Node.deleted_at >= now - LIST_DELETED_FOR))
            .order_by(Node.created_at.desc(), Node.id)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for node, agent, conns in rows:
        out.append(
            {
                "id": str(node.id),
                "name": node.name,
                "slot": node.slot,
                "location": node.location,
                "server_type": node.server_type,
                "state": node.state,
                "is_test": node.is_test,
                "ipv4": str(node.ipv4) if node.ipv4 is not None else None,  # our server's IP
                "created_at": iso(node.created_at),
                "ready_at": iso(node.ready_at),
                "agent": {
                    "registered": agent is not None,
                    "last_seen_at": iso(agent.last_seen_at) if agent else None,
                    "telemt_version": agent.telemt_version if agent else None,
                    "connections": int(conns) if conns is not None else None,
                },
            }
        )
    return out


async def node_events(db: AsyncSession, node_id: uuid.UUID) -> list[dict[str, Any]]:
    """GET /api/nodes/{id}/events: newest first (at most 200)."""
    if await db.get(Node, node_id) is None:
        raise NodeNotFound()
    rows = (
        await db.execute(
            select(NodeEvent.at, NodeEvent.kind, NodeEvent.detail)
            .where(NodeEvent.node_id == node_id)
            .order_by(NodeEvent.at.desc(), NodeEvent.id.desc())
            .limit(EVENTS_LIMIT)
        )
    ).all()
    return [{"at": iso(r.at), "kind": r.kind, "detail": dict(r.detail or {})} for r in rows]


async def live_servers(
    db: AsyncSession,
    *,
    vault_factory: Callable[[], Vault] = Vault.from_config,
    reader_factory: ReaderFactory = HetznerReader,
) -> list[dict[str, Any]]:
    """Read-only look, LIVE, at every observed node account: our servers (label managed-by) by id,
    name and labels, and only a COUNT of any other server (CLAUDE.md rule 4). For the runbook's
    "nothing of ours is left" check; changes nothing anywhere."""
    from proxyhub.accounts.hetzner_io import observe_servers
    from proxyhub.accounts.state import OBSERVED_STATES
    from proxyhub.db.models import HetznerAccount

    accounts = (
        await db.execute(
            select(HetznerAccount.id, HetznerAccount.name).where(
                HetznerAccount.state.in_([s.value for s in OBSERVED_STATES])
            )
        )
    ).all()
    vault = vault_factory()
    out: list[dict[str, Any]] = []
    for account_id, name in accounts:
        reader = await account_reader(db, vault, account_id, reader_factory)
        if reader is None:
            out.append({"account": name, "error": "token_missing"})
            continue
        try:
            obs = await call_hetzner(lambda r=reader: observe_servers(r))  # type: ignore[misc]
        except HetznerFailure as failure:
            out.append({"account": name, "error": f"{failure.kind}:{failure.code}"})
            continue
        out.append(
            {
                "account": name,
                "ours": [
                    {"hetzner_id": s.hetzner_id, "name": s.name, "labels": dict(s.labels)}
                    for s in obs.managed
                ],
                "others": obs.foreign_count,
            }
        )
    return out
