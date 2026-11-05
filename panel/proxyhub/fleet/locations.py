"""Hetzner locations scored by what Iran really reaches (P15, 2026-11-05).

The probes (proxyhub/probes/service.py) keep, per node and measuring network, the latest valid TLS
outcome from Iran together with the node's location (``probe_location_reach``). Here those rows are
read for the last :data:`WINDOW` and summed up per location and network:

* a network is ``open`` at a location when the latest outcome of at least one of our nodes there is
  open (one burned address does not close a whole location), ``closed`` when every node measured
  there is closed, and unknown without any row in the window;
* a location is ``good`` when it is open on every key network (MCI and Rightel by default), ``bad``
  when it is closed on every key network, ``unknown`` without any key-network data, else ``mixed``.

The planner (proxyhub/fleet/planner.py) prefers good locations, keeps mixed and unknown ones as a
fallback, skips bad ones, and never leaves the owner's allowed list (fleet settings). When every
allowed location is bad, it builds nothing and the owner is told once a day (proxyhub/fleet/alerts.py).

P15 on the real panel server: fsn1 closed on MCI and Rightel (443 and 8443 time out at the TLS
handshake); hel1 open on both; nbg1, ash and hil open on Rightel.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_net import ProbeLocationReach
from proxyhub.fleet.actions import iso
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.probes.settings import load_probe_settings
from proxyhub.settings_schema import FleetSettings

Db = AsyncConnection | AsyncSession
WINDOW: Final = timedelta(days=7)

ReachState = Literal["open", "closed"]
LocationClass = Literal["good", "mixed", "unknown", "bad"]
# Planner preference: lower first; ``bad`` is never used.
CLASS_RANK: Final[dict[str, int]] = {"good": 0, "mixed": 1, "unknown": 1, "bad": 2}


@dataclass(frozen=True)
class ReachRow:
    location: str
    node_id: uuid.UUID
    network_key: str
    network: str
    asn: int | None
    state: ReachState
    at: datetime


@dataclass(frozen=True)
class NetworkReach:
    key: str
    network: str
    asn: int | None
    state: ReachState
    at: datetime  # the newest outcome behind this summary
    nodes_open: int
    nodes_closed: int


@dataclass(frozen=True)
class LocationReach:
    location: str
    networks: Mapping[str, NetworkReach]

    def state(self, key: str) -> ReachState | None:
        n = self.networks.get(key)
        return n.state if n is not None else None

    def classify(self, key_networks: Sequence[str]) -> LocationClass:
        return classify([self.state(k) for k in key_networks])


def classify(states: Sequence[ReachState | None]) -> LocationClass:
    """good: open on every key network; bad: closed on every one; unknown: no data on any."""
    if not states or all(s is None for s in states):
        return "unknown"
    if all(s == "open" for s in states):
        return "good"
    if all(s == "closed" for s in states):
        return "bad"
    return "mixed"


def aggregate(rows: Iterable[ReachRow]) -> dict[str, LocationReach]:
    """Rows (one per node and network, its latest outcome) -> per location and network."""
    acc: dict[str, dict[str, list[ReachRow]]] = {}
    for r in rows:
        acc.setdefault(r.location, {}).setdefault(r.network_key, []).append(r)
    out: dict[str, LocationReach] = {}
    for location, by_key in acc.items():
        networks: dict[str, NetworkReach] = {}
        for key, items in by_key.items():
            newest = max(items, key=lambda r: r.at)
            opened = sum(1 for r in items if r.state == "open")
            networks[key] = NetworkReach(
                key=key,
                network=newest.network,
                asn=newest.asn,
                state="open" if opened else "closed",
                at=newest.at,
                nodes_open=opened,
                nodes_closed=len(items) - opened,
            )
        out[location] = LocationReach(location, networks)
    return out


async def load_location_reach(db: Db, now: datetime, window: timedelta = WINDOW) -> dict[str, LocationReach]:
    rows = (
        await db.execute(
            select(
                ProbeLocationReach.location,
                ProbeLocationReach.node_id,
                ProbeLocationReach.network_key,
                ProbeLocationReach.network,
                ProbeLocationReach.asn,
                ProbeLocationReach.state,
                ProbeLocationReach.at,
            ).where(ProbeLocationReach.at >= now - window)
        )
    ).all()
    return aggregate(
        ReachRow(
            location=r.location,
            node_id=r.node_id,
            network_key=r.network_key,
            network=r.network,
            asn=int(r.asn) if r.asn is not None else None,
            state="open" if r.state == "open" else "closed",
            at=r.at,
        )
        for r in rows
    )


def location_classes(
    reach: Mapping[str, LocationReach], locations: Iterable[str], key_networks: Sequence[str]
) -> dict[str, LocationClass]:
    return {loc: reach[loc].classify(key_networks) if loc in reach else "unknown" for loc in locations}


def reach_json(
    reach: Mapping[str, LocationReach], allowed: Sequence[str], key_networks: Sequence[str]
) -> list[dict[str, Any]]:
    """For the dashboard: every allowed location (in the owner's order), then any other location
    our nodes were measured in, with its class and its networks (key networks first)."""
    order = [*allowed, *sorted(loc for loc in reach if loc not in allowed)]
    out: list[dict[str, Any]] = []
    for loc in order:
        r = reach.get(loc)
        nets = sorted(
            (r.networks.values() if r else ()),
            key=lambda n: (
                key_networks.index(n.key) if n.key in key_networks else len(key_networks),
                n.network,
            ),
        )
        out.append(
            {
                "location": loc,
                "allowed": loc in allowed,
                "class": r.classify(key_networks) if r else "unknown",
                "networks": [
                    {
                        "key": n.key,
                        "network": n.network,
                        "asn": n.asn,
                        "state": n.state,
                        "at": iso(n.at),
                        "nodes_open": n.nodes_open,
                        "nodes_closed": n.nodes_closed,
                        "key_network": n.key in key_networks,
                    }
                    for n in nets
                ],
            }
        )
    return out


async def locations_summary(db: Db, now: datetime) -> dict[str, Any]:
    """The dashboard's location x network table (Persian labels are the dashboard's)."""
    try:
        allowed = (await load_fleet_settings(db)).locations
    except SettingsCorrupt:
        allowed = FleetSettings().locations
    keys = (await load_probe_settings(db)).key_networks
    reach = await load_location_reach(db, now)
    return {
        "window_days": WINDOW.days,
        "key_networks": keys,
        "locations": reach_json(reach, allowed, keys),
    }
