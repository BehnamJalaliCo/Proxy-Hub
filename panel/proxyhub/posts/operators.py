"""Operator names for a new post's MTProto links (decision D50: only when measured).

A link's word in a post is an operator's name («همراه اول», «رایتل», «ایرانسل») only when the
panel's OWN TLS probes from Iran (proxyhub/probes: Globalping ``http`` measurements, a real TLS
handshake to the node's own name; docs/10 section 3.11) saw the link's node open from that
operator's network, on the link's own port (the MTProto port), recently. Otherwise the word stays
«پروکسی».

* **Freshest valid result.** Per (node, port, operator) the newest probe round counts whose cell for
  that port and that operator's network (``probe_rounds.observations["https"]["cells"]``) is valid:
  "up" or "down", which the probes only record where the round's TLS control worked from the same
  network. An "invalid" cell, or none, says nothing and an older round is read instead. A fresh
  "down" wins over an older "up": no name.
* **Window: :data:`WINDOW` (6 hours).** Rounds run every 30 minutes per node, so the freshest one is
  normally minutes old and decides; the window only bounds how old a result may be when probing
  stalls (Globalping's hourly allowance running short, an outage): twelve normal rounds, still the
  same part of the day. Nothing older gives a name.
* **Operators' own networks only:** MCI (AS197207) and Rightel (AS57218). Data-centre probes never
  give a name, and neither does Irancell (AS44244): the owner wants its links named «پروکسی».
* **Up to two per operator (owner, 2026-11-06: «ایرانسل هم باشه و تا دو تا هم هر کدوم باشه»).**
  Our nodes are usually open from every operator, so a post names at most
  :data:`MAX_PER_OPERATOR` links after each operator, in the order of :data:`OPERATOR_ASNS` (MCI,
  then Rightel), each time from the links still unnamed that were seen open from that operator, in
  the post's own order. Irancell is never named (owner): its links stay «پروکسی». A link gets one
  name at most; the rest stay «پروکسی». A link never measured open from an operator never gets its
  name.

The names are decided once, when a post is made, and stored with its links (``post_links.operator``):
a later status edit never renames a link. Nothing here is about end users (rule 7): only our nodes,
measured from probe networks.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.db.models_net import ProbeRound
from proxyhub.links.contract import ProxyLink
from proxyhub.mtproto.urls import parse_key
from proxyhub.probes.settings import MCI_ASN, RIGHTEL_ASN
from proxyhub.probes.verdict import TLS_SOURCE

WINDOW: Final = timedelta(hours=6)
IRANCELL_ASN: Final = 44244  # no Globalping probe on it today (docs/10 F9)
# Operator code (post_links.operator) -> its own network, in the order a post shows them.
# Irancell is not here (owner, 2026-11-06: «ایرانسل را همون پروکسی بزنید، نیازی نیست»): a link is
# never named «ایرانسل», even once a probe measures Irancell; it stays «پروکسی».
OPERATOR_ASNS: Final[dict[str, int]] = {"mci": MCI_ASN, "rightel": RIGHTEL_ASN}
MAX_PER_OPERATOR: Final = 2
MAX_ROUNDS_PER_NODE: Final = 48  # 6 h of rounds every 30 min, twice over: a bound, never reached


def _cell(observations: Any, port: int, network_key: str) -> str | None:
    """The round's TLS cell for that port and network: "up" or "down", or None (no valid result)."""
    if not isinstance(observations, Mapping):
        return None
    tls = observations.get(TLS_SOURCE)
    cells = tls.get("cells") if isinstance(tls, Mapping) else None
    port_cells = cells.get(str(port)) if isinstance(cells, Mapping) else None
    cell = port_cells.get(network_key) if isinstance(port_cells, Mapping) else None
    return cell if cell in ("up", "down") else None


def freshest(rounds_newest_first: Sequence[Any], port: int, network_key: str) -> str | None:
    """The newest valid result ("up" / "down") of that network on that port, or None."""
    for observations in rounds_newest_first:
        cell = _cell(observations, port, network_key)
        if cell is not None:
            return cell
    return None


def open_from(rounds_newest_first: Sequence[Any], port: int) -> list[str]:
    """The operators whose freshest valid result for a link on ``port`` is "up", from its node's
    rounds of the window (newest first), in :data:`OPERATOR_ASNS` order."""
    return [
        operator
        for operator, asn in OPERATOR_ASNS.items()
        if freshest(rounds_newest_first, port, f"asn:{asn}") == "up"
    ]


def assign(open_by_key: Mapping[str, Sequence[str]], order: Sequence[str]) -> dict[str, str]:
    """key -> operator code: per operator (in :data:`OPERATOR_ASNS` order), at most
    :data:`MAX_PER_OPERATOR` of the still unnamed links (in ``order``) seen open from it."""
    named: dict[str, str] = {}
    for operator in OPERATOR_ASNS:
        given = 0
        for key in order:
            if given >= MAX_PER_OPERATOR:
                break
            if key not in named and operator in open_by_key.get(key, ()):
                named[key] = operator
                given += 1
    return named


def _port(url: str) -> int | None:
    values = parse_qs(urlsplit(url).query).get("port") or []
    return int(values[0]) if len(values) == 1 and values[0].isascii() and values[0].isdigit() else None


async def operator_labels(db: AsyncConnection, links: Sequence[ProxyLink], now: datetime) -> dict[str, str]:
    """For a NEW post: key -> operator code of each MTProto link that gets an operator's name."""
    targets: dict[str, tuple[uuid.UUID, int]] = {}
    for link in links:
        parsed = parse_key(link.key) if link.kind == "mtproto" else None
        port = _port(link.url) if parsed is not None else None
        if parsed is not None and port is not None:
            targets[link.key] = (parsed[0], port)
    if not targets:
        return {}
    rows = (
        await db.execute(
            select(ProbeRound.node_id, ProbeRound.observations)
            .where(
                ProbeRound.node_id.in_({node for node, _ in targets.values()}),
                ProbeRound.started_at >= now - WINDOW,
                ProbeRound.started_at <= now,
            )
            .order_by(ProbeRound.started_at.desc(), ProbeRound.id.desc())
            .limit(MAX_ROUNDS_PER_NODE * len(targets))
        )
    ).all()
    rounds: dict[uuid.UUID, list[Any]] = {}
    for r in rows:
        rounds.setdefault(r.node_id, []).append(r.observations)
    open_by_key = {key: open_from(rounds.get(node, []), port) for key, (node, port) in targets.items()}
    return assign(open_by_key, [link.key for link in links if link.key in targets])
