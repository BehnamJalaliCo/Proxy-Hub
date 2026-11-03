"""Read-only conversations with one Hetzner account, through ``proxyhub.hetzner.client.HetznerReader``.

Nothing here creates, changes or deletes a Hetzner resource. The write-access probe (below) sends
one request that cannot change anything by construction.

All calls are blocking (``hcloud`` uses ``requests``); async code runs them through
:func:`call_hetzner`, which moves them to a thread and bounds them with a timeout.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from hcloud import APIException

from proxyhub.hetzner.client import HetznerReader
from proxyhub.safety.budget import Price
from proxyhub.safety.gates import MANAGED_LABEL, MANAGED_VALUE, Decision, ServerView, account_guard

# Upper bound for one blocking Hetzner call including hcloud's own retries (it retries
# rate_limit_exceeded/conflict/timeouts with exponential backoff, about 31 s in total).
HETZNER_CALL_TIMEOUT_S = 90.0

FailureKind = Literal["token_invalid", "forbidden", "read_only", "rate_limited", "unreachable", "api_error"]
TokenAccess = Literal["read_write", "read_only", "invalid", "unknown"]


class HetznerFailure(Exception):
    """A Hetzner call failed. ``code`` is Hetzner's error code or an exception class name; never a secret."""

    def __init__(self, kind: FailureKind, code: str) -> None:
        super().__init__(f"hetzner {kind} ({code})")
        self.kind: FailureKind = kind
        self.code = code[:60]


def classify_api_exception(exc: APIException) -> HetznerFailure:
    code = str(exc.code)
    if code in ("unauthorized", "401"):
        return HetznerFailure("token_invalid", code)
    if code == "token_readonly":
        return HetznerFailure("read_only", code)
    if code in ("forbidden", "403"):
        return HetznerFailure("forbidden", code)
    if code in ("rate_limit_exceeded", "429"):
        return HetznerFailure("rate_limited", code)
    return HetznerFailure("api_error", code)


async def call_hetzner[R](fn: Callable[[], R], *, limit_s: float = HETZNER_CALL_TIMEOUT_S) -> R:
    """Run a blocking Hetzner call in a thread; failures become :class:`HetznerFailure`."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), limit_s)
    except TimeoutError:
        raise HetznerFailure("unreachable", "timeout") from None
    except APIException as exc:
        raise classify_api_exception(exc) from exc
    except OSError as exc:  # requests.RequestException derives from OSError
        raise HetznerFailure("unreachable", type(exc).__name__) from exc


# --- servers / account guard ------------------------------------------------------------------


def is_managed(server: ServerView) -> bool:
    return server.labels.get(MANAGED_LABEL) == MANAGED_VALUE


@dataclass(frozen=True)
class ServersObservation:
    """Every server of the project, split into ours and a *count* of the others."""

    guard: Decision
    managed: tuple[ServerView, ...]
    foreign_count: int

    @property
    def total(self) -> int:
        return len(self.managed) + self.foreign_count


def summarize_servers(servers: Iterable[ServerView]) -> ServersObservation:
    servers = list(servers)
    managed = tuple(s for s in servers if is_managed(s))
    return ServersObservation(
        guard=account_guard(servers), managed=managed, foreign_count=len(servers) - len(managed)
    )


def observe_servers(reader: HetznerReader) -> ServersObservation:
    """All servers of the project, without a label filter (the guard must see everything)."""
    return summarize_servers(reader.all_servers())


# --- live prices ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveOffer:
    server_type: str
    location: str
    price: Price
    included_traffic_bytes: int | None
    deprecated: bool
    # None: Hetzner did not say; False: listed for this location but not orderable now (finding H1).
    available: bool | None

    @property
    def orderable(self) -> bool:
        return not self.deprecated and self.available is not False


def _type_deprecated(st: Any) -> bool:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return getattr(st, "deprecation", None) is not None


def offers_from_server_types(server_types: Iterable[Any]) -> list[LiveOffer]:
    """hcloud ``ServerType`` objects -> one offer per (type, location), prices exact (net, EUR).

    Same price parsing as ``HetznerReader.server_type_offers`` plus per-location availability and
    deprecation (``server_types[].locations[]``), which that method does not expose yet.
    """
    offers: list[LiveOffer] = []
    for st in server_types:
        name = str(st.name or "")
        per_location: dict[str, Any] = {}
        for loc in st.locations or []:
            loc_name = getattr(loc.location, "name", None)
            if loc_name:
                per_location[str(loc_name)] = loc
        type_deprecated = _type_deprecated(st)
        prices: list[dict[str, Any]] = list(st.prices or [])
        for p in prices:
            location = str(p["location"])
            loc = per_location.get(location)
            available = None if loc is None or loc.available is None else bool(loc.available)
            loc_deprecated = loc is not None and loc.deprecation is not None
            traffic = p.get("included_traffic")
            offers.append(
                LiveOffer(
                    server_type=name,
                    location=location,
                    price=Price(
                        hourly=Decimal(str(p["price_hourly"]["net"])),
                        monthly=Decimal(str(p["price_monthly"]["net"])),
                    ),
                    included_traffic_bytes=int(traffic) if traffic is not None else None,
                    deprecated=type_deprecated or loc_deprecated,
                    available=available,
                )
            )
    return offers


def read_offers(reader: HetznerReader) -> list[LiveOffer]:
    return offers_from_server_types(reader.raw.server_types.get_all())


# --- read-only token detection ----------------------------------------------------------------

# Best-effort, non-destructive write probe (TO BE CONFIRMED on the panel server with a real
# read-only token; see tests/test_fleet_hetzner_real.py). It asks to rename SSH key id 0 to an
# empty name. Hetzner ids start at 1 and an empty name is invalid input, so the request cannot
# change anything whatever the token. Expected answers (docs.hetzner.cloud, "Errors"):
#   read-only token  -> 403 token_readonly ("The token is only allowed to perform GET requests")
#   read+write token -> 404 not_found or 400 invalid_input
# Anything else is "unknown" and the token is not accepted.
WRITE_PROBE_METHOD = "PUT"
WRITE_PROBE_PATH = "/ssh_keys/0"
WRITE_PROBE_BODY: dict[str, str] = {"name": ""}


def classify_write_probe(code: str | int | None) -> TokenAccess:
    """Hetzner's error code for the probe (None: the request unexpectedly succeeded)."""
    if code is None:
        return "unknown"
    c = str(code)
    if c in ("token_readonly", "forbidden", "403"):
        return "read_only"
    if c in ("unauthorized", "401"):
        return "invalid"
    if c in ("not_found", "invalid_input", "json_error", "404", "400"):
        return "read_write"
    return "unknown"


def probe_write_access(reader: HetznerReader) -> TokenAccess:
    try:
        reader.raw.request(WRITE_PROBE_METHOD, WRITE_PROBE_PATH, json=dict(WRITE_PROBE_BODY))
    except APIException as exc:
        return classify_write_probe(exc.code)
    return classify_write_probe(None)


# --- full verification of a new token ---------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    servers: ServersObservation
    # "unknown" also when the probe was skipped because the guard failed (never probe a project
    # that holds servers we do not manage).
    access: TokenAccess
    offers: list[LiveOffer] = field(default_factory=list)


def verify_token(token: str, reader_factory: Callable[[str], HetznerReader] = HetznerReader) -> Verification:
    """List ALL servers -> account guard; only if green, probe write access; then read live prices."""
    reader = reader_factory(token)
    servers = observe_servers(reader)
    if not servers.guard.allowed:
        return Verification(servers=servers, access="unknown")
    access = probe_write_access(reader)
    if access != "read_write":
        return Verification(servers=servers, access=access)
    return Verification(servers=servers, access=access, offers=read_offers(reader))
