"""Verdict logic for one node (pure; docs/10 M4.2 rules, recorded only in this round).

Per round and per source, an *observation*:

* The source must have answered for both the node and the control target.
* Only networks where the CONTROL target was reachable count ("valid networks"): a network that
  cannot reach a stable public host says nothing about our node.
* All valid networks reached the node -> ``reachable``; none did -> ``unreachable``; some did ->
  ``partial``. No valid network, a failed control, or a missing source -> ``invalid``.

The verdict uses the latest ``min_rounds`` rounds (at least 2), all younger than ``max_age``:

* any invalid observation, a missing source, too few or too old rounds -> ``unknown``
* every observation (2 sources x N rounds) ``reachable`` -> ``ok``
* every observation ``unreachable`` -> ``blocked``
* anything else (partial, or sources/rounds disagreeing) -> ``suspect``

So no single source and no single round can produce ``ok`` or ``blocked``, and a failed control
always gives ``unknown``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from proxyhub.probes.types import Measurement, NetworkObs

SOURCES: tuple[str, ...] = ("globalping", "check-host")
MIN_ROUNDS_FLOOR = 2

ObsState = Literal["reachable", "unreachable", "partial", "invalid"]
Verdict = Literal["ok", "suspect", "blocked", "unknown"]


@dataclass(frozen=True)
class SourceObservation:
    state: ObsState
    reason: str | None = None
    valid_networks: int = 0
    up_networks: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "valid_networks": self.valid_networks,
            "up_networks": self.up_networks,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SourceObservation:
        state = value.get("state")
        if state not in ("reachable", "unreachable", "partial", "invalid"):
            return cls("invalid", "unreadable")
        return cls(
            state=state,
            reason=value.get("reason"),
            valid_networks=int(value.get("valid_networks") or 0),
            up_networks=int(value.get("up_networks") or 0),
        )


@dataclass(frozen=True)
class RoundObs:
    at: datetime
    observations: Mapping[str, SourceObservation]


def control_ok(control: Measurement | None) -> bool:
    """The control proves the source works: it answered and at least one network reached it."""
    return control is not None and control.status == "ok" and any(n.state == "up" for n in control.networks)


def observe(node: Measurement | None, control: Measurement | None) -> SourceObservation:
    """One source, one round: pair each network's node result with the same network's control."""
    if node is None or control is None:
        return SourceObservation("invalid", "source_missing")
    if control.status != "ok":
        return SourceObservation("invalid", f"control_{control.status}")
    if node.status != "ok":
        return SourceObservation("invalid", f"node_{node.status}")
    if not control_ok(control):
        return SourceObservation("invalid", "control_failed")
    control_up = {n.network_key for n in control.networks if n.state == "up"}
    valid: list[NetworkObs] = [
        n for n in node.networks if n.network_key in control_up and n.state in ("up", "down")
    ]
    if not valid:
        return SourceObservation("invalid", "no_valid_networks")
    up = sum(1 for n in valid if n.state == "up")
    if up == len(valid):
        state: ObsState = "reachable"
    elif up == 0:
        state = "unreachable"
    else:
        state = "partial"
    return SourceObservation(state, None, len(valid), up)


def decide(
    rounds: Iterable[RoundObs],
    *,
    now: datetime,
    min_rounds: int = MIN_ROUNDS_FLOOR,
    max_age: timedelta = timedelta(hours=1),
    sources: Sequence[str] = SOURCES,
) -> tuple[Verdict, list[str]]:
    """Verdict and machine-readable reasons from the node's rounds (any order)."""
    need = max(min_rounds, MIN_ROUNDS_FLOOR)
    recent = sorted((r for r in rounds if now - r.at <= max_age), key=lambda r: r.at, reverse=True)[:need]
    if len(recent) < need:
        return "unknown", [f"not_enough_rounds:{len(recent)}/{need}"]
    reasons: list[str] = []
    states: list[ObsState] = []
    for i, rnd in enumerate(recent):
        for source in sources:
            obs = rnd.observations.get(source)
            if obs is None:
                reasons.append(f"round{i}:{source}:source_missing")
                continue
            if obs.state == "invalid":
                reasons.append(f"round{i}:{source}:{obs.reason or 'invalid'}")
                continue
            states.append(obs.state)
    if reasons:
        return "unknown", reasons
    if all(s == "reachable" for s in states):
        return "ok", ["all_reachable"]
    if all(s == "unreachable" for s in states):
        return "blocked", ["all_unreachable"]
    detail = sorted(
        {
            f"{source}:{rnd.observations[source].state}"
            for rnd in recent
            for source in sources
            if rnd.observations[source].state != "reachable"
        }
    )
    return "suspect", ["inconsistent", *detail]
