"""Globalping (https://api.globalping.io/v1, public, no token) from Iranian networks by ASN.

Measurement types used, and why:

* **A real TLS handshake: ``http`` with ``protocol: HTTPS``, ``HEAD /``, on the node's own DNS name
  (``slot<N>.<domain>`` / ``t<hex>.<domain>``, the SNI its links use) or, without one, its IP; on 443
  and on the MTProto port.** This is what decides (2026-11-05, P15 on the real panel server): MCI
  (AS197207) and Rightel (AS57218) answer a TCP SYN themselves for blocked addresses (a fake answer in
  about 33 ms), so a TCP "ping" reported blocked IPs as reachable; the TLS handshake then times out.
  Any HTTP status means the handshake worked (our own Xray answers 404/400 to a plain HEAD); a probe
  failure caused by the target (a timeout or a TLS error) means closed. A probe whose name resolved
  to another address than the node's says nothing about the node (``resolved_elsewhere``).
* **``ping`` with ``protocol: TCP``** on the MTProto port, and **``ping`` over ICMP**: recorded for
  context only. Neither decides anything any more (see above).
* **``dns`` A queries** for our slot names through each probe's own (ISP) resolver: an answer in
  10.10.34.0/24 is the Iranian filter's hijack signature (docs/10 section 3.10).

The control measurement is created first; the node measurements reuse ITS probes (Globalping's
``locations: <measurement id>``), so every node result is paired with the control result of the
very same probe. Rate-limit headers (``X-RateLimit-*``) are returned with every creation so the
caller can stay inside the free hourly limit (250 tests per hour per IP without a token; every
probe that runs a measurement is one test).
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


# The text Globalping's probes put into ``rawOutput`` for the failures seen from Iran (recorded
# 2026-11-05). Only a short label for the dashboard comes from it; the decision uses ``status`` and
# ``failureSource`` alone (the API says rawOutput is not meant to be parsed).
_HTTP_FAILURES: tuple[tuple[str, str], ...] = (
    ("during the TLS handshake", "tls_timeout"),
    ("while establishing the TCP connection", "tcp_timeout"),
    ("ECONNRESET", "reset"),
)


def _http_failure(raw_output: object) -> str:
    text = raw_output if isinstance(raw_output, str) else ""
    for needle, label in _HTTP_FAILURES:
        if needle in text:
            return label
    return "other"


def _ms(timings: object, key: str) -> float | None:
    if not isinstance(timings, Mapping):
        return None
    value = timings.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def parse_http(
    raw: Mapping[str, Any], requested_asns: Sequence[int] = (), expected_ip: str | None = None
) -> list[NetworkObs]:
    """An ``http`` (HTTPS, HEAD) measurement -> one entry per network. ``ok``: the probe got an HTTP
    status (so TCP and the TLS handshake worked); ``fail``: the test failed because of the target
    (a timeout, a reset, a TLS error); everything else (the probe's own failure, an unusable DNS
    answer, a name that resolved to another address than ``expected_ip``) gives no data."""
    acc: dict[str, NetworkObs] = {}
    for item in raw.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        probe, result = item.get("probe") or {}, item.get("result") or {}
        key, asn, name = _probe_key(probe)
        status = result.get("status")
        resolved = result.get("resolvedAddress")
        timings = result.get("timings")
        code = result.get("statusCode")
        if expected_ip is not None and isinstance(resolved, str) and resolved != expected_ip:
            obs = NetworkObs(key, name, asn, nodata=1, detail={"failure": "resolved_elsewhere"})
        elif status == "finished" and isinstance(code, int) and not isinstance(code, bool):
            tls = result.get("tls")
            obs = NetworkObs(
                key,
                name,
                asn,
                ok=1,
                rtt_ms=_ms(timings, "total"),
                detail={
                    "code": code,
                    "tls_ms": _ms(timings, "tls"),
                    "total_ms": _ms(timings, "total"),
                    "tls": str(tls.get("protocol"))[:16] if isinstance(tls, Mapping) else None,
                },
            )
        elif status == "failed" and result.get("failureSource") == "target" and isinstance(resolved, str):
            obs = NetworkObs(
                key,
                name,
                asn,
                fail=1,
                detail={"failure": _http_failure(result.get("rawOutput")), "tcp_ms": _ms(timings, "tcp")},
            )
        else:
            failure = result.get("failureSource") if status == "failed" else status
            obs = NetworkObs(key, name, asn, nodata=1, detail={"failure": str(failure or "unknown")[:20]})
        _merge(acc, obs)
    return [*acc.values(), *_no_probe_networks(requested_asns, acc)]


def https_body(target: str, port: int, locations: str | Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """HEAD https://<target>:<port>/ (the SNI is the target name). ``locations``: location objects
    (each with its own ``limit``) or the id of an earlier measurement whose probes are reused."""
    return {
        "type": "http",
        "target": target,
        "locations": locations if isinstance(locations, str) else [dict(loc) for loc in locations],
        "measurementOptions": {
            "protocol": "HTTPS",
            "port": port,
            "request": {"method": "HEAD", "path": "/"},
        },
    }


def location_asns(locations: Sequence[Mapping[str, Any]]) -> list[int]:
    """The ASNs named in location objects (networks that should report even without a probe)."""
    return [int(loc["asn"]) for loc in locations if isinstance(loc.get("asn"), int)]


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
        if kind == "dns":
            networks = parse_dns(raw, expected_ip, asns)
        elif kind == "https":
            networks = parse_http(raw, asns, expected_ip)
        else:
            networks = parse_ping(raw, asns)
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
