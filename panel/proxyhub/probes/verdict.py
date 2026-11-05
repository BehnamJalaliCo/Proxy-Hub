"""Verdict logic for one node (pure; docs/10 M4.2 rules).

**What decides (2026-11-05, P15):** a real TLS handshake from Iran, source ``https`` (Globalping
``http`` measurements, HTTPS ``HEAD /`` on the node's own name, on 443 and on the MTProto port). MCI
and Rightel answer a TCP SYN themselves for blocked addresses, so TCP results ("globalping" and
"check-host" TCP pings) are kept as information only and never make a node reachable again.

Per round, the ``https`` observation (:func:`observe_tls`) looks at the *key networks* (MCI and
Rightel by default) on every measured port. A cell (port x key network) counts only where the TLS
control target was reachable from that same network in the same round; otherwise it is invalid.

* every valid cell up -> ``reachable`` (at least one valid cell);
* every cell valid and down (both key networks, every port) -> ``unreachable``;
* some up, some down -> ``partial``;
* no valid cell, a failed control, a missing or failed measurement, or "down" cells next to invalid
  ones -> ``invalid`` (nothing can be concluded).

The verdict (:func:`decide`, ``sources=("https",)``) uses the latest ``min_rounds`` rounds (at
least 2), all younger than ``max_age``:

* any invalid observation, a missing source, too few or too old rounds -> ``unknown``
* every observation ``reachable`` -> ``ok``
* every observation ``unreachable`` -> ``blocked`` (TLS failed from MCI AND Rightel, on every port,
  in 2 rounds, while the control worked from both)
* anything else (partial, or rounds disagreeing) -> ``suspect``

So no single round can produce ``ok`` or ``blocked``, and a failed control always gives ``unknown``.
:func:`observe` (per TCP source, the earlier rule) is still computed for the information it gives.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from proxyhub.probes.types import Measurement, NetworkObs

# TCP sources: information only (see the module docstring).
SOURCES: tuple[str, ...] = ("globalping", "check-host")
# The source that decides: TLS handshakes from Iran.
TLS_SOURCE = "https"
TLS_SOURCES: tuple[str, ...] = (TLS_SOURCE,)
MIN_ROUNDS_FLOOR = 2

ObsState = Literal["reachable", "unreachable", "partial", "invalid"]
Verdict = Literal["ok", "suspect", "blocked", "unknown"]
CellState = Literal["up", "down", "invalid"]


@dataclass(frozen=True)
class SourceObservation:
    state: ObsState
    reason: str | None = None
    valid_networks: int = 0
    up_networks: int = 0
    # ``https`` only: port -> network key -> cell state, for every network that measured (the key
    # networks decide; the others are information for the dashboard and the location scores).
    cells: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "state": self.state,
            "reason": self.reason,
            "valid_networks": self.valid_networks,
            "up_networks": self.up_networks,
        }
        if self.cells:
            out["cells"] = {port: dict(c) for port, c in self.cells.items()}
        return out

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SourceObservation:
        state = value.get("state")
        if state not in ("reachable", "unreachable", "partial", "invalid"):
            return cls("invalid", "unreadable")
        cells = value.get("cells")
        return cls(
            state=state,
            reason=value.get("reason"),
            valid_networks=int(value.get("valid_networks") or 0),
            up_networks=int(value.get("up_networks") or 0),
            cells={
                str(port): {str(k): str(v) for k, v in c.items()}
                for port, c in (cells.items() if isinstance(cells, Mapping) else ())
                if isinstance(c, Mapping)
            },
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


def _cell(n: NetworkObs | None, control_up: set[str]) -> CellState:
    if n is None or n.network_key not in control_up or n.state == "nodata":
        return "invalid"
    return "up" if n.state == "up" else "down"


def observe_tls(
    nodes: Sequence[Measurement], control: Measurement | None, key_networks: Sequence[str]
) -> SourceObservation:
    """One round of TLS handshakes to one node (one measurement per port) against the same round's
    TLS control. Only the ``key_networks`` (e.g. ``asn:197207``, ``asn:57218``) decide."""
    if control is None or not nodes:
        return SourceObservation("invalid", "source_missing")
    if control.status != "ok":
        return SourceObservation("invalid", f"control_{control.status}")
    if not control_ok(control):
        return SourceObservation("invalid", "control_failed")
    control_up = {n.network_key for n in control.networks if n.state == "up"}
    cells: dict[str, dict[str, str]] = {}
    key_cells: list[CellState] = []
    for m in sorted(nodes, key=lambda x: x.port or 0):
        if m.status != "ok":
            return SourceObservation("invalid", f"node_{m.status}")
        by_key = {n.network_key: n for n in m.networks}
        port_cells: dict[str, str] = {}
        for key in key_networks:
            cell = _cell(by_key.get(key), control_up)
            port_cells[key] = cell
            key_cells.append(cell)
        for n in m.networks:
            if n.network_key not in port_cells and n.had_probe:
                port_cells[n.network_key] = _cell(n, control_up)
        cells[str(m.port)] = port_cells
    valid = [c for c in key_cells if c != "invalid"]
    up = sum(1 for c in valid if c == "up")
    if not valid:
        return SourceObservation("invalid", "no_valid_networks", cells=cells)
    if up == len(valid):
        state: ObsState = "reachable"
    elif up == 0 and len(valid) < len(key_cells):
        # Down where it was measured, but a key network (or a port) gave nothing: never "blocked".
        return SourceObservation("invalid", "incomplete", len(valid), 0, cells)
    elif up == 0:
        state = "unreachable"
    else:
        state = "partial"
    return SourceObservation(state, None, len(valid), up, cells)


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
