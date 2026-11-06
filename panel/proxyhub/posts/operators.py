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
* **Operators' own networks only:** MCI (AS197207), Rightel (AS57218), Irancell (AS44244).
  Data-centre probes never give a name. Globalping has no Irancell probe today, so «ایرانسل» never
  shows yet; once a probe exists the same code names it.
* **One operator, one name.** A link seen open from exactly ONE of these operators gets its name.
  Seen open from two or more, it is not one operator's link and keeps «پروکسی» (so Irancell users,
  whom no probe measures yet, are not told a working link is someone else's); seen open from none,
  or not measured, «پروکسی» too.

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
OPERATOR_ASNS: Final[dict[str, int]] = {"mci": MCI_ASN, "rightel": RIGHTEL_ASN, "irancell": IRANCELL_ASN}
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


def operator_for(rounds_newest_first: Sequence[Any], port: int) -> str | None:
    """The operator a link on ``port`` is named after, from its node's rounds of the window (newest
    first): the one operator whose freshest valid result is "up", else None (see the module doc)."""
    seen_open = [
        operator
        for operator, asn in OPERATOR_ASNS.items()
        if freshest(rounds_newest_first, port, f"asn:{asn}") == "up"
    ]
    return seen_open[0] if len(seen_open) == 1 else None


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
    out: dict[str, str] = {}
    for key, (node, port) in targets.items():
        operator = operator_for(rounds.get(node, []), port)
        if operator is not None:
            out[key] = operator
    return out
