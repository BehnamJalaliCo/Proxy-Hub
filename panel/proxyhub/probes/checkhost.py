"""check-host.net (https://check-host.net/about/api), the second, independent source: TCP connect
checks from its Iranian nodes (datacenter networks in Tehran, Isfahan, Shiraz...).

``GET /check-tcp?host=<ip>:<port>&node=...`` starts a check (``Accept: application/json``);
``GET /check-result/<request_id>`` returns, per node, ``null`` while it runs, then
``[{"time": s, "address": ip}]`` on success or ``[{"error": "..."}]`` on failure. The node list
comes from ``GET /nodes/hosts``. No token is needed; requests are kept few and spaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import httpx

from proxyhub.probes.types import Measurement, MeasurementStatus, NetworkObs, Role

API_BASE = "https://check-host.net"
USER_AGENT = "proxy-hub-panel"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
POLL_SECONDS = 2.5
MAX_WAIT_SECONDS = 40.0


@dataclass(frozen=True)
class CheckHostNode:
    name: str  # ir1.node.check-host.net
    short: str  # ir1
    city: str
    asn: int | None

    @property
    def network_key(self) -> str:
        return f"ch:{self.short}"

    @property
    def network(self) -> str:
        return f"check-host {self.short} ({self.city})"[:100]


def parse_nodes(raw: Mapping[str, Any], country: str = "ir") -> list[CheckHostNode]:
    """``/nodes/hosts`` -> the nodes in one country (the node's own IP is not kept)."""
    nodes: list[CheckHostNode] = []
    entries = raw.get("nodes")
    if not isinstance(entries, Mapping):
        return nodes
    for name, info in entries.items():
        if not isinstance(info, Mapping):
            continue
        location = info.get("location")
        if not isinstance(location, list) or not location or location[0] != country:
            continue
        asn_text = str(info.get("asn") or "")
        asn = int(asn_text[2:]) if asn_text.startswith("AS") and asn_text[2:].isdigit() else None
        city = str(location[2]) if len(location) > 2 else ""
        nodes.append(CheckHostNode(name=str(name), short=str(name).split(".")[0], city=city, asn=asn))
    return sorted(nodes, key=lambda n: n.name)


def parse_tcp_result(raw: Mapping[str, Any], nodes: Sequence[CheckHostNode]) -> list[NetworkObs]:
    out: list[NetworkObs] = []
    for node in nodes:
        value = raw.get(node.name)
        key, name, asn = node.network_key, node.network, node.asn
        first = value[0] if isinstance(value, list) and value else None
        if not isinstance(first, Mapping):
            out.append(NetworkObs(key, name, asn, nodata=1, detail={"pending": value is None}))
        elif "error" in first:
            out.append(NetworkObs(key, name, asn, fail=1, detail={"error": str(first.get("error"))[:60]}))
        elif isinstance(first.get("time"), int | float):
            out.append(NetworkObs(key, name, asn, ok=1, rtt_ms=round(float(first["time"]) * 1000, 1)))
        else:
            out.append(NetworkObs(key, name, asn, nodata=1))
    return out


def classify_start(status_code: int, body: Any) -> tuple[MeasurementStatus, str | None]:
    """Answer of ``/check-tcp``: ``{"ok": 1, "request_id": ...}`` when started. check-host throttles
    repeated checks of the same target with HTTP 200 and ``{"error": "limit_exceeded"}`` (seen
    2026-11-03 after a few checks of one host within a minute)."""
    if status_code == 429:
        return "rate_limited", None
    if isinstance(body, Mapping):
        if body.get("error") == "limit_exceeded":
            return "rate_limited", None
        request_id = body.get("request_id")
        if body.get("ok") == 1 and isinstance(request_id, str) and request_id.isalnum():
            return "ok", request_id
    return "error", None


def _complete(raw: Mapping[str, Any], nodes: Sequence[CheckHostNode]) -> bool:
    return all(raw.get(n.name) is not None for n in nodes)


class CheckHostClient:
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
            raise RuntimeError("CheckHostClient used outside 'async with'")
        return self._http

    async def iran_nodes(self) -> list[CheckHostNode] | None:
        """None when check-host could not be reached."""
        try:
            response = await self._client().get("/nodes/hosts")
            if response.status_code != 200:
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return parse_nodes(body) if isinstance(body, dict) else None

    async def tcp(
        self,
        host: str,
        port: int,
        nodes: Sequence[CheckHostNode],
        *,
        role: Role,
        node_id: Any = None,
    ) -> Measurement:
        at = datetime.now(UTC)
        if not nodes:
            return Measurement("check-host", "tcp", role, host, port, "no_probes", at, node_id=node_id)
        params: list[tuple[str, str | int | float | bool | None]] = [("host", f"{host}:{port}")]
        params += [("node", n.name) for n in nodes]
        status_code = 0
        try:
            response = await self._client().get("/check-tcp", params=params)
            status_code = response.status_code
            body = response.json() if status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            body = None
        started, request_id = classify_start(status_code, body)
        if request_id is None:
            return Measurement("check-host", "tcp", role, host, port, started, at, node_id=node_id)
        status, raw = await self._wait(request_id, nodes)
        if raw is None:
            return Measurement(
                "check-host", "tcp", role, host, port, status, at, ref=request_id, node_id=node_id
            )
        final: MeasurementStatus = "ok" if status in ("ok", "timeout") else status
        return Measurement(
            "check-host",
            "tcp",
            role,
            host,
            port,
            final,
            at,
            ref=request_id,
            note="partial" if status == "timeout" else None,
            networks=tuple(parse_tcp_result(raw, nodes)),
            node_id=node_id,
        )

    async def _wait(
        self, request_id: str, nodes: Sequence[CheckHostNode]
    ) -> tuple[MeasurementStatus, dict[str, Any] | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_wait
        last: dict[str, Any] | None = None
        while True:
            await asyncio.sleep(self._poll)
            try:
                response = await self._client().get(f"/check-result/{request_id}")
                body = response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                body = None
            if isinstance(body, dict):
                last = body
                if _complete(body, nodes):
                    return "ok", body
            if loop.time() + self._poll > deadline:
                return ("timeout", last) if last is not None else ("error", None)
