"""Fleet planner: the desired fleet for the 9 + 1 slot model (decision D14) -> intended actions.

Pure function of a snapshot (database rows + the last live Hetzner observations), so it is
tested exhaustively with real-shaped data. Every intended creation is evaluated through
``proxyhub.safety.gates.can_create`` with the real projected month cost
(``proxyhub.safety.budget`` + live prices) and every intended deletion through ``can_delete``.

Model (docs/10_IMPLEMENTATION_PLAN.md section 3.11):
* ``target_slots`` fixed slots (default 9), spread over the owner's locations (3 fsn1, 3 nbg1,
  3 hel1 with the defaults).
* One spare: when a slot's node burns (blocked from Iran by both probe sources) or stays unhealthy
  past its repair window, the replacement is built FIRST, in another location, and the old node is
  retired only after the replacement has been ready for a short settle time (the slot's DNS name
  follows the ready node by itself). The fleet never plans more than ``target_slots + 1`` live nodes.
* A slot whose last request was refused by the executor waits a little before asking again; a
  server that got a burned IP (proxyhub/fleet/ip_history.py) is retried in another location.
* Accounts: youngest active account first, the next one when it is full (section 3.8).
* Intended creations are evaluated cumulatively, in slot order: an allowed creation counts
  against the node cap, the hourly creation cap, the account and the month budget of the next one.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from proxyhub.accounts.hetzner_io import LiveOffer
from proxyhub.fleet.costs import NodeCostInput, PriceKey, money, node_cost_items
from proxyhub.fleet.lifecycle import LIVE_STATES, NodeState
from proxyhub.safety.budget import Price, projected_month_total, rest_of_month_cost
from proxyhub.safety.gates import (
    DELETABLE_ROLES,
    AccountView,
    CreateRequest,
    NodeRowView,
    ServerView,
    can_create,
    can_delete,
)
from proxyhub.settings_schema import FleetSettings

SPARE_SLOTS = 1  # D14: 9 active + 1 spare
OBSERVATION_MAX_AGE = timedelta(minutes=30)  # account_guard checks every 10 minutes
# The pricing loop refreshes offers hourly. Older prices may be lower than today's (Hetzner raised
# prices twice in 2026), so budget projections built on them could let a creation exceed the cap.
OFFER_MAX_AGE = timedelta(hours=6)
REPLACEMENT_SETTLE = timedelta(minutes=2)  # DNS TTL 60 s + margin (section 3.11 step 4)
CREATION_WINDOW = timedelta(hours=1)
# An unhealthy node (agent silent, Telemt down) gets this long to recover by itself (the agent and
# Telemt restart under systemd) before a replacement is built.
REPAIR_WINDOW = timedelta(minutes=15)
# A probe verdict older than this no longer decides anything (probes/settings.py max_round_age_s).
VERDICT_MAX_AGE = timedelta(hours=1)
# After the executor refused a slot's request (a gate said no on its live look), wait before asking again.
REQUEST_BACKOFF = timedelta(minutes=15)

S = NodeState
STARTING = frozenset({S.REQUESTED, S.CREATING, S.BOOTING, S.REGISTERING})
SERVING = frozenset({S.READY})
SICK = frozenset({S.UNHEALTHY, S.REPAIRING})

ActionKind = Literal["create", "delete", "none"]


@dataclass(frozen=True, kw_only=True)
class AccountSnapshot:
    id: str
    name: str
    state: str
    created_at: datetime
    learned_server_limit: int | None = None
    observed_at: datetime | None = None  # last SUCCESSFUL live observation
    guard_ok: bool | None = None
    servers_total: int | None = None


@dataclass(frozen=True, kw_only=True)
class NodeSnapshot(NodeCostInput):
    id: str
    slot: int | None = None
    is_test: bool = False
    ready_at: datetime | None = None
    # Latest probe verdict from Iran (proxyhub/probes) and when it was computed.
    verdict: str | None = None
    verdict_at: datetime | None = None
    # When the node entered its current state (node_operations.state_since).
    state_since: datetime | None = None
    retire_reason: str | None = None
    ipv4: str | None = None  # our server's public address (never a user's)
    # False only for a request closed before any Hetzner create call (it created nothing).
    create_attempted: bool = True


def counts_as_creation(n: NodeSnapshot, now: datetime) -> bool:
    """A server creation within the last hour (the hourly creation cap). A request the executor
    closed before any create call made no server and does not count."""
    if n.created_at <= now - CREATION_WINDOW:
        return False
    return not (n.state == NodeState.DELETED and n.hetzner_id is None and not n.create_attempted)


def verdict_blocked(n: NodeSnapshot, now: datetime) -> bool:
    """A READY node whose fresh probe verdict says blocked from Iran (both sources, >= 2 rounds)."""
    return (
        n.state == NodeState.READY
        and n.verdict == "blocked"
        and n.verdict_at is not None
        and now - n.verdict_at <= VERDICT_MAX_AGE
    )


@dataclass(frozen=True)
class AccountOffer:
    account_id: str
    offer: LiveOffer
    # When the pricing loop stored this offer. The database always has it (NOT NULL); None only in
    # pure planner tests that do not model price age.
    fetched_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class FleetSnapshot:
    now: datetime
    settings: FleetSettings
    accounts: tuple[AccountSnapshot, ...] = ()
    nodes: tuple[NodeSnapshot, ...] = ()
    offers: tuple[AccountOffer, ...] = ()
    # Our own servers (label managed-by=proxy-hub) as last seen live, by (account_id, hetzner_id).
    servers: Mapping[tuple[str, int], ServerView] = field(default_factory=dict)
    settings_problem: str | None = None
    # Reasons found outside the database that block every creation (e.g. agent_domain_not_set: a
    # node's user data would have nowhere to register, so the executor would refuse each request).
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedAction:
    action: ActionKind
    target: str
    allowed: bool
    reasons: tuple[str, ...]
    context: Mapping[str, Any] = field(default_factory=dict)

    def contract_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
        }

    def stored_json(self) -> dict[str, Any]:
        return {**self.contract_json(), "context": dict(self.context)}

    def summary(self, dry_run: bool) -> str:
        prefix = "dry-run: " if dry_run else ""
        c = self.context
        if self.action == "create":
            what = (
                f"create {c.get('server_type') or '?'} in {c.get('location') or '?'}"
                f" (account {c.get('account_name') or '-'})"
            )
        elif self.action == "delete":
            what = f"delete {self.target} ({c.get('why', '')})"
        else:
            what = f"no action on {self.target}"
        done = "would do" if dry_run else "doing (the node executor checks the gates again)"
        verdict = done if self.allowed else "blocked: " + ", ".join(self.reasons)
        return f"{prefix}{what}; {verdict}"


@dataclass(frozen=True)
class FleetPlan:
    generated_at: datetime
    dry_run: bool
    actions: tuple[PlannedAction, ...]
    projected_month_eur: Decimal
    projected_after_eur: Decimal
    live_nodes: int
    missing_prices: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """Hash of the decisions only (costs drift every hour and must not count as a change)."""
        payload = [[a.action, a.target, a.allowed, list(a.reasons)] for a in self.actions]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def summary(self) -> dict[str, Any]:
        creates = [a for a in self.actions if a.action == "create"]
        return {
            "projected_month_eur": money(self.projected_month_eur),
            "projected_after_eur": money(self.projected_after_eur),
            "live_nodes": self.live_nodes,
            "creates_allowed": sum(1 for a in creates if a.allowed),
            "creates_blocked": sum(1 for a in creates if not a.allowed),
            "deletes": sum(1 for a in self.actions if a.action == "delete"),
            "missing_prices": list(self.missing_prices),
        }


def _d(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001")))


@dataclass
class _Running:
    """Totals as they would be after the allowed actions planned so far."""

    live_total: int  # every live node, test nodes included (caps)
    fleet_live: int  # live slot-model nodes (spare rule)
    creations: int  # creations in the last hour
    projected: Decimal
    per_account: dict[str, int]
    per_location: dict[str, int]


@dataclass(frozen=True)
class _Placement:
    account: AccountSnapshot | None
    location: str | None
    offer: LiveOffer | None
    reasons: tuple[str, ...]


class _Planner:
    def __init__(self, snap: FleetSnapshot, dry_run: bool) -> None:
        self.snap = snap
        self.s = snap.settings
        self.now = snap.now
        self.dry_run = dry_run
        self.limits = self.s.limits()
        allowed_types = set(self.s.allowed_server_types)
        self.offers: dict[str, dict[str, list[LiveOffer]]] = defaultdict(lambda: defaultdict(list))
        self.prices: dict[PriceKey, Price] = {}
        for ao in snap.offers:
            o = ao.offer
            self.prices[(ao.account_id, o.server_type, o.location)] = o.price
            if o.server_type in allowed_types and o.orderable:
                self.offers[ao.account_id][o.location].append(o)
        for by_loc in self.offers.values():
            for lst in by_loc.values():
                lst.sort(key=lambda o: (o.price.monthly, o.price.hourly, o.server_type))

        live = [n for n in snap.nodes if n.state in LIVE_STATES]
        self.fleet_nodes = [n for n in live if not n.is_test]
        items, missing = node_cost_items(snap.nodes, self.prices, self.now)
        self.missing_prices = tuple(missing)
        per_account_db: dict[str, int] = defaultdict(int)
        for n in live:
            per_account_db[n.account_id] += 1
        per_location: dict[str, int] = defaultdict(int)
        for n in self.fleet_nodes:
            per_location[n.location] += 1
        self.run = _Running(
            live_total=len(live),
            fleet_live=len(self.fleet_nodes),
            creations=sum(1 for n in snap.nodes if counts_as_creation(n, self.now)),
            projected=projected_month_total(items, self.now),
            per_account={a.id: max(a.servers_total or 0, per_account_db.get(a.id, 0)) for a in snap.accounts},
            per_location=per_location,
        )
        self.projected_start = self.run.projected
        self.global_reasons: list[str] = []
        if snap.settings_problem:
            self.global_reasons.append("settings_invalid")
        self.global_reasons.extend(r for r in snap.blockers if r not in self.global_reasons)
        if missing:
            self.global_reasons.append("projection_incomplete")
        if not self.s.locations:
            self.global_reasons.append("no_location_configured")
        if any(ao.fetched_at is not None and self.now - ao.fetched_at > OFFER_MAX_AGE for ao in snap.offers):
            self.global_reasons.append("prices_stale")  # fail closed until the pricing loop recovers
        self.accounts_by_id = {a.id: a for a in snap.accounts}

    # --- accounts and placement -------------------------------------------------------------

    def _fresh(self, a: AccountSnapshot) -> bool:
        return a.observed_at is not None and self.now - a.observed_at <= OBSERVATION_MAX_AGE

    def _account_view(self, a: AccountSnapshot) -> AccountView:
        return AccountView(
            id=a.id,
            state=a.state,
            guard_ok=self._fresh(a) and a.guard_ok is True,
            learned_server_limit=a.learned_server_limit,
            servers_in_account=self.run.per_account.get(a.id, 0),
        )

    def _eligible(self, a: AccountSnapshot) -> bool:
        view = self._account_view(a)
        limit = view.learned_server_limit
        return view.guard_ok and (limit is None or view.servers_in_account + 1 <= limit)

    def _cheapest(self, account_id: str, location: str) -> LiveOffer | None:
        lst = self.offers.get(account_id, {}).get(location)
        return lst[0] if lst else None

    def _place(self, preferred: list[str]) -> _Placement:
        active = sorted(
            (a for a in self.snap.accounts if a.state == "active"), key=lambda a: a.created_at, reverse=True
        )
        first_loc = preferred[0] if preferred else None
        if not active:
            return _Placement(None, first_loc, None, ("no_active_account",))
        fallback: tuple[AccountSnapshot, str, LiveOffer] | None = None
        for a in active:
            for loc in preferred:
                offer = self._cheapest(a.id, loc)
                if offer is None:
                    continue
                if self._eligible(a):
                    return _Placement(a, loc, offer, ())
                if fallback is None:
                    fallback = (a, loc, offer)
        if fallback is not None:
            a, loc, offer = fallback
            reasons = () if self._fresh(a) else ("account_observation_stale",)
            return _Placement(a, loc, offer, reasons)
        return _Placement(active[0], first_loc, None, ("no_live_price",))

    def _slot_locations(self, slot: int) -> list[str]:
        locs = list(self.s.locations)
        if not locs:
            return []
        start = (slot - 1) % len(locs)
        return [locs[(start + k) % len(locs)] for k in range(len(locs))]

    def _replacement_locations(self, avoid: set[str]) -> list[str]:
        locs = list(self.s.locations)
        others = [loc for loc in locs if loc not in avoid]
        return sorted(others, key=lambda loc: (self.run.per_location.get(loc, 0), locs.index(loc)))

    def _repair_over(self, n: NodeSnapshot) -> bool:
        since = n.state_since or n.created_at
        return self.now - since > REPAIR_WINDOW

    # --- evaluations --------------------------------------------------------------------------

    def _create(
        self, slot: int, kind: str, avoid: set[str] | None = None, prefer_not: set[str] | None = None
    ) -> PlannedAction:
        """``avoid``: locations of the nodes being replaced (never reused, D14). ``prefer_not``:
        locations tried recently (a server that got a burned IP); used only if nothing else is left."""
        planner_reasons = list(self.global_reasons)
        if self.run.fleet_live + 1 > self.s.target_slots + SPARE_SLOTS:
            planner_reasons.append("spare_in_use")
        soft = prefer_not or set()
        if avoid is None and not soft:
            preferred = self._slot_locations(slot)
        else:
            hard = avoid or set()
            preferred = self._replacement_locations(hard | soft) or self._replacement_locations(hard)
            if not preferred and self.s.locations:
                if hard:
                    planner_reasons.append("no_other_location")
                    preferred = list(self.s.locations)
                else:
                    preferred = self._slot_locations(slot)
        placement = self._place(preferred)
        planner_reasons.extend(r for r in placement.reasons if r not in planner_reasons)

        cost = rest_of_month_cost(placement.offer.price, self.now) if placement.offer else Decimal(0)
        if placement.account is not None:
            view = self._account_view(placement.account)
        else:
            view = AccountView(
                id="-", state="none", guard_ok=False, learned_server_limit=None, servers_in_account=0
            )
        req = CreateRequest(
            account=view,
            fleet_live_total=self.run.live_total,
            creations_last_hour=self.run.creations,
            projected_month_eur=self.run.projected,
            new_server_rest_of_month_eur=cost,
        )
        decision = can_create(self.limits, req)
        reasons = planner_reasons + [r for r in decision.reasons if r not in planner_reasons]
        allowed = decision.allowed and not planner_reasons
        context: dict[str, Any] = {
            "slot": slot,
            "kind": kind,
            "account_id": placement.account.id if placement.account else None,
            "account_name": placement.account.name if placement.account else None,
            "location": placement.location,
            "server_type": placement.offer.server_type if placement.offer else None,
            "price_monthly_eur": _d(placement.offer.price.monthly) if placement.offer else None,
            "price_hourly_eur": _d(placement.offer.price.hourly) if placement.offer else None,
            "rest_of_month_eur": _d(cost),
            "projected_month_eur": _d(self.run.projected),
            "fleet_live_total": self.run.live_total,
            "creations_last_hour": self.run.creations,
            "servers_in_account": view.servers_in_account,
            "avoid": sorted((avoid or set()) | soft),
        }
        if allowed and placement.account is not None and placement.location is not None:
            self.run.live_total += 1
            self.run.fleet_live += 1
            self.run.creations += 1
            self.run.projected += cost
            self.run.per_account[placement.account.id] = self.run.per_account.get(placement.account.id, 0) + 1
            self.run.per_location[placement.location] = self.run.per_location.get(placement.location, 0) + 1
        return PlannedAction("create", f"slot-{slot}", allowed, tuple(reasons), context)

    def _delete(self, node: NodeSnapshot, why: str) -> PlannedAction:
        """Evaluated as the executor would see it right after the lifecycle step to ``retiring``."""
        planner_reasons: list[str] = []
        if self.limits.kill_switch:
            planner_reasons.append("kill_switch_on")
        account = self.accounts_by_id.get(node.account_id)
        if account is None or account.state not in ("active", "draining", "preparing"):
            planner_reasons.append("account_not_usable")
        server: ServerView | None = None
        no_server = node.hetzner_id is None and node.state in (S.REQUESTED, S.CREATING, S.RETIRING)
        if node.hetzner_id is None and not no_server:
            planner_reasons.append("no_hetzner_id")
        elif node.hetzner_id is not None:
            server = self.snap.servers.get((node.account_id, node.hetzner_id))
            if server is None:
                planner_reasons.append("server_not_observed")
        gate_reasons: tuple[str, ...] = ()
        # A node without any server (a request, or a create that never answered) is closed by the
        # executor after its lookups by label: no delete gate is involved until a server is found.
        gate_allowed = no_server
        if server is not None:
            d = can_delete(
                server, NodeRowView(node.account_id, node.hetzner_id, S.RETIRING.value), node.account_id
            )
            gate_reasons, gate_allowed = d.reasons, d.allowed
        reasons = planner_reasons + [r for r in gate_reasons if r not in planner_reasons]
        context = {
            "why": why,
            "node_id": node.id,
            "account_id": node.account_id,
            "hetzner_id": node.hetzner_id,
            "state": node.state.value,
            "slot": node.slot,
            "location": node.location,
        }
        return PlannedAction(
            "delete", node.name, gate_allowed and not planner_reasons, tuple(reasons), context
        )

    def _slot_history(self, slot: int) -> list[NodeSnapshot]:
        return [
            n
            for n in self.snap.nodes
            if n.slot == slot and not n.is_test and n.state == S.DELETED and n.deleted_at is not None
        ]

    def _backing_off(self, slot: int) -> bool:
        """The executor refused this slot's last request a moment ago (no server was made)."""
        return any(
            n.hetzner_id is None
            and not n.create_attempted
            and n.deleted_at is not None
            and self.now - n.deleted_at < REQUEST_BACKOFF
            for n in self._slot_history(slot)
        )

    def _burned_ip_locations(self, slot: int) -> set[str]:
        """Locations where this slot's server got a burned IP within the last hour."""
        return {
            n.location
            for n in self._slot_history(slot)
            if n.retire_reason == "burned_ip"
            and n.deleted_at is not None
            and self.now - n.deleted_at < CREATION_WINDOW
        }

    @staticmethod
    def _none(target: str, reason: str, **context: Any) -> PlannedAction:
        return PlannedAction("none", target, False, (reason,), context)

    # --- the plan -----------------------------------------------------------------------------

    def build(self) -> FleetPlan:
        actions: list[PlannedAction] = []
        deletes: list[tuple[NodeSnapshot, str]] = []
        by_slot: dict[int, list[NodeSnapshot]] = defaultdict(list)
        excess: list[NodeSnapshot] = []
        for n in self.fleet_nodes:
            if n.slot is not None and 1 <= n.slot <= self.s.target_slots:
                by_slot[n.slot].append(n)
            else:
                excess.append(n)

        for slot in range(1, self.s.target_slots + 1):
            nodes = by_slot.get(slot, [])
            blocked = [n for n in nodes if n.state == S.BURNED or verdict_blocked(n, self.now)]
            serving = [n for n in nodes if n.state in SERVING and n not in blocked]
            starting = [n for n in nodes if n.state in STARTING]
            sick = [n for n in nodes if n.state in SICK]
            leaving = [n for n in nodes if n.state in (S.DRAINING, S.RETIRING)]
            target = f"slot-{slot}"
            if serving:
                # The slot's DNS name points at the newest ready node; every other node of the slot
                # (burned, unhealthy, an older ready one) leaves once that node has settled.
                keep = max(serving, key=lambda n: (n.ready_at or n.created_at, n.created_at, n.id))
                others = [*blocked, *sick, *(n for n in serving if n is not keep)]
                if not others:
                    actions.append(self._none(target, "slot_healthy", slot=slot))
                elif (keep.ready_at or keep.created_at) <= self.now - REPLACEMENT_SETTLE:
                    for n in others:
                        why = "burned" if n in blocked else "unhealthy" if n in sick else "duplicate"
                        deletes.append((n, why))
                    actions.append(self._none(target, "slot_healthy", slot=slot))
                else:
                    actions.append(self._none(target, "replacement_settling", slot=slot))
            elif starting:
                reason = "replacement_in_progress" if blocked or sick or leaving else "slot_starting"
                actions.append(self._none(target, reason, slot=slot))
            elif sick and not all(self._repair_over(n) for n in sick):
                actions.append(self._none(target, "slot_unhealthy", slot=slot))
            else:
                # Nothing serves and nothing is on its way: build one. A node that never served
                # (failed boot) is not worth keeping while its replacement is built.
                deletes.extend((n, "unhealthy") for n in sick if n.ready_at is None)
                if self._backing_off(slot):
                    actions.append(self._none(target, "request_backoff", slot=slot))
                    continue
                avoid = {n.location for n in blocked + sick}
                prefer_not = {n.location for n in leaving} | self._burned_ip_locations(slot)
                if blocked or sick or leaving:
                    actions.append(self._create(slot, "replace", avoid=avoid, prefer_not=prefer_not))
                else:
                    actions.append(self._create(slot, "fill", prefer_not=prefer_not))

        for n in excess:
            if n.state not in (S.RETIRING, S.DRAINING):
                deletes.append((n, "excess"))
            elif n.state == S.DRAINING:
                actions.append(self._none(n.name, "draining", node_id=n.id))
        for n in self.fleet_nodes:
            if n.state == S.RETIRING:
                deletes.append((n, "retiring"))
        actions.extend(self._delete(n, why) for n, why in deletes)

        known = {
            (n.account_id, n.hetzner_id)
            for n in self.snap.nodes
            if n.hetzner_id is not None and n.state != S.DELETED
        }
        for (account_id, hetzner_id), server in sorted(self.snap.servers.items()):
            if (account_id, hetzner_id) in known:
                continue
            role = server.labels.get("role", "")
            actions.append(
                self._none(
                    f"server:{hetzner_id}",
                    "orphan_server_not_in_db",
                    account_id=account_id,
                    name=server.name,
                    role=role,
                    deletable_role=role in DELETABLE_ROLES,
                )
            )

        return FleetPlan(
            generated_at=self.now,
            dry_run=self.dry_run,
            actions=tuple(actions),
            projected_month_eur=self.projected_start,
            projected_after_eur=self.run.projected,
            live_nodes=len([n for n in self.snap.nodes if n.state in LIVE_STATES]),
            missing_prices=self.missing_prices,
        )


def build_plan(snap: FleetSnapshot, *, dry_run: bool = True) -> FleetPlan:
    return _Planner(snap, dry_run).build()
