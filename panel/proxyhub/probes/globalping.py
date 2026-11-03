"""Globalping (https://api.globalping.io/v1, public, no token) from Iranian networks by ASN.

Measurement types used, and why:

* **Reachability of the MTProto port: ``ping`` with ``protocol: TCP`` and ``port`` = the node's
  Telemt port.** Each "packet" is a real TCP connection attempt (SYN, SYN/ACK) to exactly the port
  Telegram clients open, from eyeball networks inside Iran. Iran blocks a proxy IP by dropping its
  packets, which is exactly a failed handshake here; RTT and loss come per attempt. It costs one
  test per probe.
* **``ping`` over ICMP**: recorded for context only (ICMP is filtered independently of TCP, so it
  never decides anything).
* **TLS handshake: not used for verdicts.** Globalping's ``http`` measurement has no SNI option
  (``request.host`` only overrides the Host header; SNI follows the target), so it cannot send the
  fake-TLS domain our ``ee`` secrets use to an IP target. Whether that is enough is field test F5.
* **``dns`` A queries** for our slot names through each probe's own (ISP) resolver: an answer in
  10.10.34.0/24 is the Iranian filter's hijack signature (docs/10 section 3.10).

The control measurement is created first; the node measurements reuse ITS probes (Globalping's
``locations: <measurement id>``), so every node result is paired with the control result of the
very same probe. Rate-limit headers (``X-RateLimit-*``) are returned with every creation so the
caller can stay inside the free hourly limit (250 tests per hour per IP without a token).
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx

from proxyhub.probes.types import IRAN_NETWORKS_FA, Kind, Measurement, MeasurementStatus, NetworkObs, Role

API_BASE = "https://api.globalping.io/v1"
USER_AGENT = "proxy-hub-panel"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HIJACK_NET = ipaddress.ip_network("10.10.34.0/24")
TCP_PACKETS = 3
POLL_SECONDS = 2.0
MAX_WAIT_SECONDS = 60.0  # probe-side timeout is at most 30 s; Globalping asks for 10 s on top


@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    reset_at: datetime


@dataclass(frozen=True)
class Created:
    status: MeasurementStatus
    id: str | None
    rate: RateLimit | None


def parse_rate_limit(headers: Mapping[str, str], now: datetime) -> RateLimit | None:
    try:
        return RateLimit(
            limit=int(headers["x-ratelimit-limit"]),
            remaining=int(headers["x-ratelimit-remaining"]),
            reset_at=now + timedelta(seconds=int(headers["x-ratelimit-reset"])),
        )
    except (KeyError, ValueError):
        return None


def network_name(asn: int | None, fallback: str) -> str:
    if asn is not None and asn in IRAN_NETWORKS_FA:
        return IRAN_NETWORKS_FA[asn]
    return (fallback or f"AS{asn}")[:100]


def _probe_key(probe: Mapping[str, Any]) -> tuple[str, int | None, str]:
    asn = probe.get("asn")
    asn_i = int(asn) if isinstance(asn, int) else None
    return f"asn:{asn_i}", asn_i, network_name(asn_i, str(probe.get("network") or ""))


def _merge(acc: dict[str, NetworkObs], obs: NetworkObs) -> None:
    prev = acc.get(obs.network_key)
    if prev is None:
        acc[obs.network_key] = obs
        return
    rtts = [r for r in (prev.rtt_ms, obs.rtt_ms) if r is not None]
    acc[obs.network_key] = NetworkObs(
        network_key=prev.network_key,
        network=prev.network,
        asn=prev.asn,
        ok=prev.ok + obs.ok,
        fail=prev.fail + obs.fail,
        nodata=prev.nodata + obs.nodata,
        rtt_ms=min(rtts) if rtts else None,
        detail={**prev.detail, "probes": int(prev.detail.get("probes", 1)) + 1},
    )


def _no_probe_networks(requested_asns: Sequence[int], seen: Mapping[str, NetworkObs]) -> list[NetworkObs]:
    return [
        NetworkObs(network_key=f"asn:{a}", network=network_name(a, ""), asn=a, detail={"no_probe": True})
        for a in requested_asns
        if f"asn:{a}" not in seen
    ]


def parse_ping(raw: Mapping[str, Any], requested_asns: Sequence[int] = ()) -> list[NetworkObs]:
    """A ``ping`` measurement (TCP or ICMP) -> one entry per network. Every TCP "packet" is one
    connection attempt: ``ok`` = attempts answered, ``fail`` = attempts lost. A test that failed for
    a reason on the probe's side (``failureSource`` internal/resolver, or missing) gives no data."""
    acc: dict[str, NetworkObs] = {}
    for item in raw.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        probe, result = item.get("probe") or {}, item.get("result") or {}
        key, asn, name = _probe_key(probe)
        status = result.get("status")
        if status == "finished":
            stats = result.get("stats") or {}
            rcv = int(stats.get("rcv") or 0)
            total = int(stats.get("total") or 0)
            dropped = stats.get("drop")
            drop = int(dropped) if isinstance(dropped, int) else max(total - rcv, 0)
            avg = stats.get("avg")
            obs = NetworkObs(
                key,
                name,
                asn,
                ok=rcv,
                fail=drop,
                rtt_ms=float(avg) if isinstance(avg, int | float) else None,
                detail={"loss": stats.get("loss")},
            )
        elif status == "failed" and result.get("failureSource") == "target":
            obs = NetworkObs(key, name, asn, fail=1, detail={"failure": "target"})
        else:
            obs = NetworkObs(
                key, name, asn, nodata=1, detail={"failure": str(result.get("failureSource") or status)}
            )
        _merge(acc, obs)
    return [*acc.values(), *_no_probe_networks(requested_asns, acc)]


def parse_dns(
    raw: Mapping[str, Any], expected_ip: str | None, requested_asns: Sequence[int] = ()
) -> list[NetworkObs]:
    """A ``dns`` A measurement of one of our names. ``fail`` = hijacked (answer in 10.10.34.0/24);
    ``ok`` = an answer that is not hijacked (``matches`` says whether it is the node's IP)."""
    acc: dict[str, NetworkObs] = {}
    for item in raw.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        probe, result = item.get("probe") or {}, item.get("result") or {}
        key, asn, name = _probe_key(probe)
        if result.get("status") != "finished":
            _merge(acc, NetworkObs(key, name, asn, nodata=1, detail={"failure": str(result.get("status"))}))
            continue
        answers: list[str] = []
        for ans in result.get("answers") or []:
            if isinstance(ans, Mapping) and ans.get("type") == "A" and isinstance(ans.get("value"), str):
                answers.append(ans["value"])
        hijacked = any(_in_hijack_net(a) for a in answers)
        detail = {
            "rcode": str(result.get("statusCodeName") or ""),
            "answers": answers[:4],
            "hijacked": hijacked,
            "matches": expected_ip in answers if expected_ip else None,
        }
        if hijacked:
            obs = NetworkObs(key, name, asn, fail=1, detail=detail)
        elif answers:
            obs = NetworkObs(key, name, asn, ok=1, detail=detail)
        else:
            obs = NetworkObs(key, name, asn, nodata=1, detail=detail)
        _merge(acc, obs)
    return [*acc.values(), *_no_probe_networks(requested_asns, acc)]


def _in_hijack_net(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in HIJACK_NET
    except ValueError:
        return False


def tcp_body(target: str, port: int, locations: str | Sequence[int]) -> dict[str, Any]:
    return {
        "type": "ping",
        "target": target,
        "locations": _locations(locations),
        "measurementOptions": {"protocol": "TCP", "port": port, "packets": TCP_PACKETS},
    }


def icmp_body(target: str, locations: str | Sequence[int]) -> dict[str, Any]:
    return {
        "type": "ping",
        "target": target,
        "locations": _locations(locations),
        "measurementOptions": {"packets": TCP_PACKETS},
    }


def dns_body(name: str, locations: str | Sequence[int]) -> dict[str, Any]:
    return {
        "type": "dns",
        "target": name,
        "locations": _locations(locations),
        "measurementOptions": {"query": {"type": "A"}},
    }


def _locations(locations: str | Sequence[int]) -> str | list[dict[str, int]]:
    if isinstance(locations, str):
        return locations  # reuse the probes of an earlier measurement (the control)
    return [{"asn": int(a), "limit": 1} for a in locations]


class GlobalpingClient:
    def __init__(
        self,
        *,
        base_url: str = API_BASE,
        timeout: httpx.Timeout = TIMEOUT,
        poll_seconds: float = POLL_SECONDS,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._poll = poll_seconds
        self._max_wait = max_wait_seconds
        self._http: httpx.AsyncClient | None = None
        self.last_rate: RateLimit | None = None

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("GlobalpingClient used outside 'async with'")
        return self._http

    async def create(self, body: Mapping[str, Any]) -> Created:
        try:
            response = await self._client().post("/measurements", json=dict(body))
        except httpx.HTTPError:
            return Created("error", None, None)
        rate = parse_rate_limit(response.headers, datetime.now(UTC))
        if rate is not None:
            self.last_rate = rate
        if response.status_code == 202:
            try:
                mid = response.json().get("id")
            except ValueError:
                mid = None
            return (
                Created("ok", str(mid), rate)
                if isinstance(mid, str) and mid
                else Created("error", None, rate)
            )
        if response.status_code == 422:
            return Created("no_probes", None, rate)
        if response.status_code == 429:
            return Created("rate_limited", None, rate)
        return Created("error", None, rate)

    async def wait(self, measurement_id: str) -> tuple[MeasurementStatus, dict[str, Any] | None]:
        """Poll until the measurement is final. On timeout, the partial result is returned."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_wait
        last: dict[str, Any] | None = None
        while True:
            delay = self._poll
            try:
                response = await self._client().get(f"/measurements/{measurement_id}")
                if response.status_code == 200:
                    body = response.json()
                    if isinstance(body, dict):
                        last = body
                        if body.get("status") != "in-progress":
                            return "ok", body
                elif response.status_code == 429:
                    delay = min(float(response.headers.get("retry-after", "2") or 2), 10.0)
                elif response.status_code == 404:
                    return "error", None
            except (httpx.HTTPError, ValueError):
                pass
            if loop.time() + delay > deadline:
                return ("timeout", last) if last is not None else ("error", None)
            await asyncio.sleep(delay)

    async def run(
        self,
        body: Mapping[str, Any],
        *,
        kind: Kind,
        role: Role,
        target: str,
        port: int | None,
        requested_asns: Sequence[int],
        expected_ip: str | None = None,
        node_id: Any = None,
    ) -> tuple[Measurement, str | None]:
        """Create, wait, parse. Returns the measurement and its Globalping id (for probe reuse)."""
        at = datetime.now(UTC)
        created = await self.create(body)
        if created.status != "ok" or created.id is None:
            return (
                Measurement("globalping", kind, role, target, port, created.status, at, node_id=node_id),
                None,
            )
        status, raw = await self.wait(created.id)
        if raw is None:
            return Measurement(
                "globalping", kind, role, target, port, status, at, ref=created.id, node_id=node_id
            ), created.id
        # With reused probes the same ASNs are missing as in the control: list them too.
        asns = requested_asns
        networks = parse_dns(raw, expected_ip, asns) if kind == "dns" else parse_ping(raw, asns)
        # A partial result after a timeout still counts for the probes that finished.
        final: MeasurementStatus = "ok" if status in ("ok", "timeout") else status
        return (
            Measurement(
                "globalping",
                kind,
                role,
                target,
                port,
                final,
                at,
                ref=created.id,
                note="partial" if status == "timeout" else None,
                networks=tuple(networks),
                node_id=node_id,
            ),
            created.id,
        )
