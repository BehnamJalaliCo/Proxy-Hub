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
        self._client = Client(token=token, application_name=USER_AGENT)

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
