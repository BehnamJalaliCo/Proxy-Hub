"""Which candidate becomes the REALITY target / the fake-TLS domain in ``auto`` mode (pure).

Inputs per site (``target_states``): its last two rounds of TLS handshakes from Iran, per network
``up``, ``down`` or ``nodata`` (a network counts only where the round's TLS control worked), and
when our nodes' own TLS 1.3 + HTTP/2 check of it last passed or failed (``target_ok``).

* **Open from Iran**: the latest round (at most :data:`FRESH` old) is up on at least one key network
  (MCI, Rightel) and down on none.
* **Blocked from Iran**: down on the same key network in the latest two rounds.
* **Node check**: ``passed`` when the newest node verdict is a pass, ``failed`` when it is a fail
  (within :data:`NODE_MEMORY`), else unknown (our nodes check only the target their key carries).

REALITY (:func:`choose_reality`): the current target stays while it is a candidate, not blocked from
Iran and not failed by the nodes (no churn: links already posted keep their target until they end).
Otherwise the best candidate that is open from Iran and not failed by the nodes: one the nodes
passed first, then the one open on more key networks, then the faster one, then the owner's order.
The nodes then check the new target; only a target they pass ever gets configs
(proxyhub/reality/policy.py), and one they fail is replaced at the next pass here.

MTProto (:func:`choose_mtproto`, nodes without their own DNS name only): every ``ee`` link of those
nodes carries the domain, so the current one is replaced only when it FAILS: blocked from Iran. The
replacement is the best candidate open from Iran.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

FRESH: Final = timedelta(hours=24)
NODE_MEMORY: Final = timedelta(days=7)

NetState = Literal["up", "down", "nodata"]
NodeCheck = Literal["passed", "failed"]
Why = Literal["not_a_candidate", "node_check_failed", "blocked_from_iran"]


@dataclass(frozen=True)
class NetResult:
    state: NetState
    network: str = ""
    asn: int | None = None
    ms: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"state": self.state, "network": self.network, "asn": self.asn, "ms": self.ms}

    @classmethod
    def from_json(cls, value: Any) -> NetResult:
        if not isinstance(value, Mapping):
            return cls("nodata")
        state = value.get("state")
        ms = value.get("ms")
        asn = value.get("asn")
        return cls(
            state=state if state in ("up", "down", "nodata") else "nodata",
            network=str(value.get("network") or ""),
            asn=asn if isinstance(asn, int) else None,
            ms=float(ms) if isinstance(ms, int | float) and not isinstance(ms, bool) else None,
        )


@dataclass(frozen=True)
class Candidate:
    host: str
    measured_at: datetime | None = None
    iran: Mapping[str, NetResult] = field(default_factory=dict)
    prev_measured_at: datetime | None = None
    iran_prev: Mapping[str, NetResult] = field(default_factory=dict)
    node_ok_at: datetime | None = None
    node_fail_at: datetime | None = None


@dataclass(frozen=True)
class IranView:
    open_on: tuple[str, ...]
    down_on: tuple[str, ...]
    blocked_on: tuple[str, ...]  # down in the latest two rounds
    ms: float | None  # the latest round's mean time on the key networks that were up
    fresh: bool


def iran_view(c: Candidate | None, keys: Sequence[str], now: datetime) -> IranView:
    if c is None or c.measured_at is None or now - c.measured_at > FRESH:
        return IranView((), (), (), None, False)
    open_on = tuple(k for k in keys if (r := c.iran.get(k)) is not None and r.state == "up")
    down_on = tuple(k for k in keys if (r := c.iran.get(k)) is not None and r.state == "down")
    prev_ok = c.prev_measured_at is not None and c.measured_at - c.prev_measured_at <= FRESH
    blocked_on = tuple(
        k for k in down_on if prev_ok and (p := c.iran_prev.get(k)) is not None and p.state == "down"
    )
    times = [r.ms for k in open_on if (r := c.iran.get(k)) is not None and r.ms is not None]
    return IranView(open_on, down_on, blocked_on, sum(times) / len(times) if times else None, True)


def node_check(c: Candidate | None, now: datetime) -> NodeCheck | None:
    if c is None:
        return None
    ok = c.node_ok_at if c.node_ok_at is not None and now - c.node_ok_at <= NODE_MEMORY else None
    fail = c.node_fail_at if c.node_fail_at is not None and now - c.node_fail_at <= NODE_MEMORY else None
    if ok is None and fail is None:
        return None
    if fail is None or (ok is not None and ok >= fail):
        return "passed"
    return "failed"


def open_from_iran(c: Candidate | None, keys: Sequence[str], now: datetime) -> bool:
    v = iran_view(c, keys, now)
    return v.fresh and bool(v.open_on) and not v.down_on


@dataclass(frozen=True)
class Choice:
    host: str  # what the setting should hold now (the current one when nothing changes)
    switch: bool
    why: Why | None = None  # why the current one is not kept (also when no replacement exists)
    networks: tuple[str, ...] = ()  # blocked_from_iran: the key networks it was blocked on


def _bad(
    current: str, listed: Sequence[str], states: Mapping[str, Candidate], keys: Sequence[str], now: datetime
) -> tuple[Why | None, tuple[str, ...]]:
    cur = states.get(current)
    if current not in listed:
        return "not_a_candidate", ()
    if node_check(cur, now) == "failed":
        return "node_check_failed", ()
    blocked = iran_view(cur, keys, now).blocked_on
    if blocked:
        return "blocked_from_iran", blocked
    return None, ()


def _best(
    pool: Sequence[str],
    listed: Sequence[str],
    states: Mapping[str, Candidate],
    keys: Sequence[str],
    now: datetime,
    *,
    prefer_node_passed: bool,
) -> str | None:
    def rank(host: str) -> tuple[int, int, float, int]:
        c = states.get(host)
        v = iran_view(c, keys, now)
        passed = 0 if (prefer_node_passed and node_check(c, now) == "passed") else 1
        return (passed, -len(v.open_on), v.ms if v.ms is not None else math.inf, listed.index(host))

    return min(pool, key=rank) if pool else None


def choose_reality(
    current: str, listed: Sequence[str], states: Mapping[str, Candidate], keys: Sequence[str], now: datetime
) -> Choice:
    why, networks = _bad(current, listed, states, keys, now)
    if why is None:
        return Choice(current, False)
    pool = [
        h
        for h in listed
        if h != current
        and open_from_iran(states.get(h), keys, now)
        and node_check(states.get(h), now) != "failed"
    ]
    best = _best(pool, listed, states, keys, now, prefer_node_passed=True)
    if best is None:
        return Choice(current, False, why, networks)
    return Choice(best, True, why, networks)


def choose_mtproto(
    current: str, listed: Sequence[str], states: Mapping[str, Candidate], keys: Sequence[str], now: datetime
) -> Choice:
    blocked = iran_view(states.get(current), keys, now).blocked_on
    if not blocked:
        return Choice(current, False)
    pool = [h for h in listed if h != current and open_from_iran(states.get(h), keys, now)]
    best = _best(pool, listed, states, keys, now, prefer_node_passed=False)
    if best is None:
        return Choice(current, False, "blocked_from_iran", blocked)
    return Choice(best, True, "blocked_from_iran", blocked)


def confirm_due(c: Candidate | None, keys: Sequence[str], now: datetime, after: timedelta) -> bool:
    """A current target looked closed on a key network in its latest round, which the round before
    does not confirm: measure it again soon (two rounds decide)."""
    v = iran_view(c, keys, now)
    if c is None or c.measured_at is None or not v.fresh or not v.down_on:
        return False
    return set(v.blocked_on) != set(v.down_on) and now - c.measured_at >= after
