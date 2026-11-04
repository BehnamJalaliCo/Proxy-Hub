"""Is a domain filtered in Iran? Pure logic, no I/O (decision D35, docs/10 section 3.10).

**Measurement.** A Globalping DNS (A) query of the zone's apex name from up to
:data:`DOMAIN_PROBES` probes in Iran, each through its own (ISP or datacenter) resolver. The Iranian
filter answers ANY name of a filtered domain with an address in 10.10.34.0/24, so no record is
needed on the zone. Per probe:

* **hijacked**: an A answer in 10.10.34.0/24;
* **answered** (clean when not hijacked): the resolver gave a final answer, i.e. A records, or
  NOERROR / NXDOMAIN without any (an apex without an A record is a clean answer);
* **no answer**: the probe failed or timed out, or the resolver failed (SERVFAIL, REFUSED, ...).

**Verdict** over the stored rounds of the zone. A round in which no probe answered says nothing
about the domain and is skipped:

* ``filtered``: the latest 2 rounds, at least :data:`MIN_GAP` apart and both younger than
  :data:`MAX_AGE`, EACH have >= :data:`MIN_HIJACKED` hijacked answers from >= :data:`MIN_NETWORKS`
  distinct networks AND hijacked >= 50% of the probes that answered;
* ``clear``: the latest round has answers and none of them is hijacked;
* ``unknown``: anything else. Nothing is ever done on ``unknown``, and never on a single round.

**Cadence.** The active domain every :data:`ACTIVE_EVERY`, standby domains every
:data:`STANDBY_EVERY`; a domain whose latest round showed a hijacked answer is checked again after
:data:`CONFIRM_EVERY` (otherwise a standby domain could never collect two rounds within 6 hours).

Nothing here is about a user (rule 7): only counts per measurement network.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from proxyhub.probes.globalping import HIJACK_NET, RateLimit, network_name

DOMAIN_PROBES: Final = 4  # Globalping tests per check (one per probe)
COUNTRY: Final = "IR"
MIN_HIJACKED: Final = 2
MIN_NETWORKS: Final = 2
MIN_GAP: Final = timedelta(minutes=20)
MAX_AGE: Final = timedelta(hours=6)
ACTIVE_EVERY: Final = timedelta(minutes=30)
STANDBY_EVERY: Final = timedelta(hours=6)
CONFIRM_EVERY: Final = timedelta(minutes=30)
ANSWER_RCODES: Final = frozenset({"NOERROR", "NXDOMAIN"})

Verdict = Literal["filtered", "clear", "unknown"]
Outcome = Literal["hijacked", "clean"]


@dataclass(frozen=True)
class NetworkCount:
    """What one measurement network (by ASN) answered in one round."""

    key: str
    asn: int | None
    network: str
    answered: int = 0
    hijacked: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "asn": self.asn,
            "network": self.network,
            "answered": self.answered,
            "hijacked": self.hijacked,
        }


@dataclass(frozen=True)
class DomainRound:
    """One filter check of one domain (stored in ``domain_checks``)."""

    at: datetime
    status: str = "ok"  # ok | no_probes | rate_limited | error | timeout
    probes_asked: int = DOMAIN_PROBES
    probes: int = 0
    answered: int = 0
    hijacked: int = 0
    networks_answered: int = 0
    networks_hijacked: int = 0
    networks: tuple[NetworkCount, ...] = field(default=())
    ref: str | None = None

    @property
    def informative(self) -> bool:
        return self.answered > 0

    @property
    def hijack_round(self) -> bool:
        """This round alone looks filtered (never enough to act on)."""
        return (
            self.hijacked >= MIN_HIJACKED
            and self.networks_hijacked >= MIN_NETWORKS
            and self.hijacked * 2 >= self.answered
        )

    def detail_json(self) -> list[dict[str, Any]]:
        return [n.to_json() for n in self.networks]


def domain_dns_body(name: str) -> dict[str, Any]:
    """Globalping measurement request: an A query of ``name`` from up to 4 probes in Iran."""
    return {
        "type": "dns",
        "target": name,
        "locations": [{"country": COUNTRY, "limit": DOMAIN_PROBES}],
        "measurementOptions": {"query": {"type": "A"}},
    }


def _hijack_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in HIJACK_NET
    except ValueError:
        return False


def classify(result: Any) -> Outcome | None:
    """One probe's DNS result -> hijacked, clean, or None (no usable answer)."""
    if not isinstance(result, Mapping) or result.get("status") != "finished":
        return None
    answers = [
        str(a["value"])
        for a in result.get("answers") or []
        if isinstance(a, Mapping) and a.get("type") == "A" and isinstance(a.get("value"), str)
    ]
    if any(_hijack_address(a) for a in answers):
        return "hijacked"
    if answers or str(result.get("statusCodeName") or "").upper() in ANSWER_RCODES:
        return "clean"
    return None


def parse_domain_round(
    raw: Mapping[str, Any], at: datetime, *, ref: str | None = None, status: str = "ok"
) -> DomainRound:
    """A finished (or partial, after a timeout) Globalping ``dns`` measurement -> one round."""
    probes = 0
    acc: dict[str, NetworkCount] = {}
    for item in raw.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        probes += 1
        outcome = classify(item.get("result"))
        if outcome is None:
            continue
        probe = item.get("probe") or {}
        asn = probe.get("asn") if isinstance(probe, Mapping) else None
        asn_i = int(asn) if isinstance(asn, int) and not isinstance(asn, bool) else None
        fallback = str(probe.get("network") or "") if isinstance(probe, Mapping) else ""
        key = f"asn:{asn_i}" if asn_i is not None else f"net:{fallback[:60]}"
        prev = acc.get(key) or NetworkCount(key, asn_i, network_name(asn_i, fallback))
        acc[key] = NetworkCount(
            key,
            prev.asn,
            prev.network,
            answered=prev.answered + 1,
            hijacked=prev.hijacked + (1 if outcome == "hijacked" else 0),
        )
    networks = tuple(sorted(acc.values(), key=lambda n: (n.asn is None, n.asn or 0, n.network)))
    return DomainRound(
        at=at,
        status=status,
        probes=probes,
        answered=sum(n.answered for n in networks),
        hijacked=sum(n.hijacked for n in networks),
        networks_answered=len(networks),
        networks_hijacked=sum(1 for n in networks if n.hijacked > 0),
        networks=networks,
        ref=ref,
    )


def failed_round(at: datetime, status: str, *, ref: str | None = None) -> DomainRound:
    """A check that produced no result (rate limit, no probe in Iran, network error)."""
    return DomainRound(at=at, status=status, ref=ref)


def domain_verdict(rounds: Iterable[DomainRound], now: datetime) -> tuple[Verdict, list[str]]:
    """``filtered`` | ``clear`` | ``unknown`` with the reasons for ``unknown`` (see module doc)."""
    useful = sorted((r for r in rounds if r.informative), key=lambda r: r.at, reverse=True)
    if not useful:
        return "unknown", ["no_answers"]
    latest = useful[0]
    if latest.hijacked == 0:
        return "clear", []
    if not latest.hijack_round:
        return "unknown", ["weak_signal"]
    if len(useful) < 2:
        return "unknown", ["single_round"]
    previous = useful[1]
    if not previous.hijack_round:
        return "unknown", ["not_confirmed"]
    if latest.at - previous.at < MIN_GAP:
        return "unknown", ["rounds_too_close"]
    if now - previous.at >= MAX_AGE or now - latest.at >= MAX_AGE:  # both strictly younger
        return "unknown", ["rounds_too_old"]
    return "filtered", []


def suspicious(rounds: Iterable[DomainRound]) -> bool:
    """The latest round with answers had at least one hijacked answer."""
    useful = sorted((r for r in rounds if r.informative), key=lambda r: r.at, reverse=True)
    return bool(useful) and useful[0].hijacked > 0


def check_interval(pool_state: str, is_suspicious: bool) -> timedelta | None:
    """How often a zone in this pool state is checked; None: never (not in rotation)."""
    if pool_state == "active":
        base = ACTIVE_EVERY
    elif pool_state == "standby":
        base = STANDBY_EVERY
    else:
        return None
    return min(base, CONFIRM_EVERY) if is_suspicious else base


def check_due(pool_state: str, last_at: datetime | None, is_suspicious: bool, now: datetime) -> bool:
    every = check_interval(pool_state, is_suspicious)
    if every is None:
        return False
    return last_at is None or now - last_at >= every


def budget_allows(rate: RateLimit | None, now: datetime, reserve: int) -> bool:
    """Run a check only if Globalping's hourly allowance still covers it AND ``reserve`` tests for
    the node probes' next full cycle (they are never starved). An unknown or expired window is
    fine: the measurement's own headers tell the truth again."""
    if rate is None or rate.reset_at <= now:
        return True
    return rate.remaining >= DOMAIN_PROBES + reserve


def tighter_rate(a: RateLimit | None, b: RateLimit | None, now: datetime) -> RateLimit | None:
    """Of two observations of the same allowance, the one that leaves fewer tests now."""
    live = [r for r in (a, b) if r is not None and r.reset_at > now]
    if not live:
        return a or b
    return min(live, key=lambda r: r.remaining)
