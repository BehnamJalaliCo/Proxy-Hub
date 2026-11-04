"""Read-only usage of OUR Hetzner servers: traffic counters and the metrics API.

Two requests, both GET, through the same ``hcloud`` client as :class:`~proxyhub.hetzner.client.HetznerReader`:

* ``GET /servers?label_selector=managed-by=proxy-hub``: ``outgoing_traffic``, ``ingoing_traffic`` and
  ``included_traffic`` (bytes, current billing period), the vCPU count and the price of extra traffic.
  The label selector means servers we do not manage are never even listed (CLAUDE.md rule 4).
* ``GET /servers/{id}/metrics?type=cpu,network``: CPU percent and public-interface bytes per second.
  Hetzner's CPU value is per vCPU (100 = one full vCPU; docs.hetzner.cloud, "Get Metrics for a
  Server"), so it is divided by the vCPU count to get percent of the whole server.

Nothing here creates, changes or deletes anything. Calls are blocking (``requests``); async code runs
them through :func:`proxyhub.accounts.hetzner_io.call_hetzner`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from hcloud.servers import Server

from proxyhub.hetzner.client import HetznerReader
from proxyhub.safety.gates import MANAGED_LABEL, MANAGED_VALUE

MANAGED_SELECTOR: Final = f"{MANAGED_LABEL}={MANAGED_VALUE}"
METRIC_TYPES: Final = ("cpu", "network")
CPU_SERIES: Final = "cpu"
NET_OUT_SERIES: Final = "network.0.bandwidth.out"
NET_IN_SERIES: Final = "network.0.bandwidth.in"
METRICS_STEP_S: Final = 60


@dataclass(frozen=True)
class ServerUsage:
    """Traffic of one of our servers in the current billing period, as Hetzner counts it."""

    hetzner_id: int
    labels: dict[str, str]
    location: str | None
    out_bytes: int | None
    in_bytes: int | None
    included_bytes: int | None
    cores: int | None
    traffic_price_per_tb_eur: Decimal | None


@dataclass(frozen=True)
class LoadWindow:
    """CPU and bandwidth of one server over one window (percent of the whole server; bytes/s)."""

    start: datetime
    end: datetime
    step_s: float
    cpu_avg: float | None
    cpu_max: float | None
    net_out_bps_avg: float | None
    net_in_bps_avg: float | None
    points: int


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d >= 0 else None


def _traffic_price(prices: Iterable[Mapping[str, Any]], location: str | None) -> Decimal | None:
    """Net EUR per extra TB at the server's location (``server_type.prices[].price_per_tb_traffic``)."""
    for p in prices:
        if location is not None and p.get("location") == location:
            per_tb = p.get("price_per_tb_traffic")
            if isinstance(per_tb, Mapping):
                return _decimal_or_none(per_tb.get("net"))
    return None


def usage_from_server(server: Any) -> ServerUsage:
    """hcloud ``BoundServer`` (or ``Server``) -> :class:`ServerUsage`. Reads only these fields."""
    location = getattr(getattr(server, "location", None), "name", None)
    server_type = getattr(server, "server_type", None)
    cores = getattr(server_type, "cores", None)
    prices: list[Mapping[str, Any]] = list(getattr(server_type, "prices", None) or [])
    return ServerUsage(
        hetzner_id=int(server.id or 0),
        labels={str(k): str(v) for k, v in dict(server.labels or {}).items()},
        location=str(location) if location else None,
        out_bytes=_int_or_none(getattr(server, "outgoing_traffic", None)),
        in_bytes=_int_or_none(getattr(server, "ingoing_traffic", None)),
        included_bytes=_int_or_none(getattr(server, "included_traffic", None)),
        cores=int(cores) if isinstance(cores, int | float) and cores >= 1 else None,
        traffic_price_per_tb_eur=_traffic_price(prices, str(location) if location else None),
    )


def read_managed_usage(reader: HetznerReader) -> list[ServerUsage]:
    """Our servers only: listed by label, and checked again here (a label we did not ask for is dropped)."""
    servers = reader.raw.servers.get_all(label_selector=MANAGED_SELECTOR)
    usage = [usage_from_server(s) for s in servers]
    return [u for u in usage if u.hetzner_id > 0 and u.labels.get(MANAGED_LABEL) == MANAGED_VALUE]


def _values(series: Mapping[str, Any], name: str) -> list[float]:
    """Finite numbers of one time series (Hetzner sends ``[[timestamp, "value"], ...]``)."""
    raw = series.get(name)
    if not isinstance(raw, Mapping):
        return []
    out: list[float] = []
    for point in raw.get("values") or []:
        if not isinstance(point, list | tuple) or len(point) != 2:
            continue
        try:
            v = float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v >= 0:
            out.append(v)
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def load_from_metrics(metrics: Any, cores: int | None) -> LoadWindow:
    """hcloud ``Metrics`` -> :class:`LoadWindow`. CPU is divided by ``cores`` (unknown: by 1)."""
    series: Mapping[str, Any] = dict(getattr(metrics, "time_series", None) or {})
    divisor = float(cores) if cores and cores >= 1 else 1.0
    cpu = [min(v / divisor, 100.0) for v in _values(series, CPU_SERIES)]
    out = _values(series, NET_OUT_SERIES)
    inn = _values(series, NET_IN_SERIES)
    return LoadWindow(
        start=metrics.start,
        end=metrics.end,
        step_s=float(metrics.step),
        cpu_avg=_mean(cpu),
        cpu_max=max(cpu) if cpu else None,
        net_out_bps_avg=_mean(out),
        net_in_bps_avg=_mean(inn),
        points=max(len(cpu), len(out), len(inn)),
    )


def read_load(
    reader: HetznerReader, hetzner_id: int, start: datetime, end: datetime, cores: int | None
) -> LoadWindow:
    """GET /servers/{id}/metrics for cpu and network over [start, end], one point a minute."""
    response = reader.raw.servers.get_metrics(
        Server(id=hetzner_id), type=list(METRIC_TYPES), start=start, end=end, step=METRICS_STEP_S
    )
    return load_from_metrics(response.metrics, cores)
