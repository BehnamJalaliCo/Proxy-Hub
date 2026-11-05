"""Value types shared by the probe sources, the verdict logic and storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Source = Literal["globalping", "check-host"]
Kind = Literal["tcp", "icmp", "dns", "https"]
# node: one of our nodes; control: a public host that proves the probes work; target: a candidate
# REALITY / fake-TLS site (proxyhub/targets), measured without any node.
Role = Literal["node", "control", "target"]
# ok: the source answered (results may still say "unreachable"); the others mean "no usable data".
MeasurementStatus = Literal["ok", "no_probes", "rate_limited", "error", "timeout", "skipped"]

# Persian names of the Iranian networks the owner asked about (docs/10 I4, section 3.11).
IRAN_NETWORKS_FA: dict[int, str] = {
    197207: "همراه اول",
    57218: "رایتل",
    44244: "ایرانسل",
    58224: "مخابرات ایران",
}


@dataclass(frozen=True)
class NetworkObs:
    """What one network saw: connection attempts (or DNS answers) that worked, failed, or gave no
    data (probe offline, internal probe failure). ``ok == fail == nodata == 0`` means the network
    had no probe at all in this measurement."""

    network_key: str  # 'asn:197207' or 'ch:ir1'
    network: str
    asn: int | None
    ok: int = 0
    fail: int = 0
    nodata: int = 0
    rtt_ms: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> Literal["up", "down", "nodata"]:
        """One successful handshake means the network is not blocked from reaching the target."""
        if self.ok > 0:
            return "up"
        if self.fail > 0:
            return "down"
        return "nodata"

    @property
    def had_probe(self) -> bool:
        return self.ok + self.fail + self.nodata > 0


@dataclass(frozen=True)
class Measurement:
    source: Source
    kind: Kind
    role: Role
    target: str
    port: int | None
    status: MeasurementStatus
    at: datetime
    ref: str | None = None
    note: str | None = None
    networks: tuple[NetworkObs, ...] = ()
    node_id: Any = None  # uuid.UUID for role 'node'
