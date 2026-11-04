"""Read-only access to one Hetzner account (one project token).

Mutations live in ``proxyhub.hetzner.mutations`` and require a permit from
``proxyhub.safety.gates``. An architecture test fails if any other module calls them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hcloud import Client

from proxyhub.safety.budget import Price
from proxyhub.safety.gates import ServerView

USER_AGENT = "proxy-hub-panel"
# Every HTTP request to Hetzner is bounded (hcloud's default is no timeout at all, so one stuck
# connection could hold a worker thread forever). Seconds: TCP/TLS connect, then each read.
# hcloud retries a timed-out request up to 5 times with backoff; callers bound the whole call with
# proxyhub.accounts.hetzner_io.call_hetzner. A create that timed out may still have happened on
# Hetzner's side: the node executor finds such a server by its node-id label and never creates a
# second one (server names are unique per project, so a retried POST is refused as well).
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
# Polling a Hetzner action (e.g. waiting for a server to start) at most 2 minutes.
POLL_INTERVAL_S = 1.0
POLL_MAX_RETRIES = 120


@dataclass(frozen=True)
class ServerTypeOffer:
    name: str
    location: str
    price: Price
    included_traffic_bytes: int | None
    deprecated: bool


def _delete_protected(protection: Any) -> bool:
    prot: dict[str, Any] = dict(protection or {})
    return bool(prot.get("delete", False))


class HetznerReader:
    def __init__(self, token: str) -> None:
        self._client = Client(
            token=token,
            application_name=USER_AGENT,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            poll_interval=POLL_INTERVAL_S,
            poll_max_retries=POLL_MAX_RETRIES,
        )

    @property
    def raw(self) -> Client:
        return self._client

    def all_servers(self) -> list[ServerView]:
        """Every server in the project, without a label filter (needed by the account guard)."""
        return [
            ServerView(
                hetzner_id=int(s.id or 0),
                name=str(s.name or ""),
                labels=dict(s.labels or {}),
                delete_protected=_delete_protected(s.protection),
            )
            for s in self._client.servers.get_all()
        ]

    def server_type_offers(self) -> list[ServerTypeOffer]:
        """Live prices per server type and location (net, monthly and hourly)."""
        offers: list[ServerTypeOffer] = []
        for st in self._client.server_types.get_all():
            prices: list[dict[str, Any]] = list(st.prices or [])
            for p in prices:
                offers.append(
                    ServerTypeOffer(
                        name=str(st.name or ""),
                        location=p["location"],
                        price=Price(
                            hourly=Decimal(p["price_hourly"]["net"]),
                            monthly=Decimal(p["price_monthly"]["net"]),
                        ),
                        included_traffic_bytes=p.get("included_traffic"),
                        deprecated=st.deprecation is not None,
                    )
                )
        return offers
