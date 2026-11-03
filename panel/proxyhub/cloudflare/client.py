"""Cloudflare API v4 client (httpx), limited to what the panel needs: verify the token, list zones,
list/create/update/delete DNS records.

Rule 3: the token is a :class:`~proxyhub.vault.SecretValue`, revealed only while building the
Authorization header of each request. URLs never contain it (httpx logs request URLs at INFO), and
:class:`CloudflareError` carries only an error kind, the HTTP status and Cloudflare's numeric error
codes: never a message, a header or a request object.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self

import httpx

from proxyhub.vault import SecretValue

API_BASE = "https://api.cloudflare.com/client/v4"
USER_AGENT = "proxy-hub-panel"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
ZONES_PER_PAGE = 50
RECORDS_PER_PAGE = 500
MAX_PAGES = 40
# Cloudflare API tokens are 40 URL-safe characters today; allow some room for future formats but
# never whitespace or anything that could break the header.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.]{20,200}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")

ErrorKind = Literal["denied", "not_found", "rejected", "rate_limited", "unavailable", "invalid_response"]


class CloudflareError(Exception):
    """A failed Cloudflare call. ``str()`` is safe to log: kind, status and numeric codes only."""

    def __init__(self, kind: ErrorKind, status: int | None = None, codes: tuple[int, ...] = ()) -> None:
        self.kind: ErrorKind = kind
        self.status = status
        self.codes = codes
        super().__init__(f"cloudflare {kind} (status={status}, codes={list(codes)})")


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    status: str
    account_id: str | None


@dataclass(frozen=True)
class DnsRecord:
    id: str
    zone_id: str
    name: str
    type: str
    content: str
    ttl: int
    proxied: bool
    comment: str | None


@dataclass(frozen=True)
class RecordSpec:
    """What the panel writes: everything a managed record has (the comment carries the marker)."""

    name: str
    type: str
    content: str
    ttl: int
    proxied: bool
    comment: str

    def body(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "content": self.content,
            "ttl": self.ttl,
            "proxied": self.proxied,
            "comment": self.comment,
        }


def valid_token_format(token: str) -> bool:
    return bool(TOKEN_RE.fullmatch(token))


def _codes(body: Mapping[str, Any]) -> tuple[int, ...]:
    errors = body.get("errors")
    out: list[int] = []
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, Mapping) and isinstance(err.get("code"), int):
                out.append(int(err["code"]))
    return tuple(out)


def _str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CloudflareError("invalid_response")
    return value


def parse_zone(raw: Mapping[str, Any]) -> Zone:
    """One element of ``GET /zones`` ``result``."""
    account = raw.get("account")
    account_id = account.get("id") if isinstance(account, Mapping) else None
    zone_id = _str(raw.get("id"), "id")
    if not HEX32.fullmatch(zone_id):
        raise CloudflareError("invalid_response")
    return Zone(
        id=zone_id,
        name=_str(raw.get("name"), "name").lower().rstrip("."),
        status=str(raw.get("status") or "unknown")[:20],
        account_id=account_id if isinstance(account_id, str) else None,
    )


def parse_record(zone_id: str, raw: Mapping[str, Any]) -> DnsRecord:
    """One element of ``GET /zones/{zone_id}/dns_records`` ``result`` (any record type)."""
    record_id = _str(raw.get("id"), "id")
    ttl = raw.get("ttl")
    comment = raw.get("comment")
    return DnsRecord(
        id=record_id,
        zone_id=zone_id,
        name=_str(raw.get("name"), "name").lower().rstrip("."),
        type=_str(raw.get("type"), "type").upper(),
        content=str(raw.get("content") or ""),
        ttl=int(ttl) if isinstance(ttl, int | float) else 1,
        proxied=bool(raw.get("proxied", False)),
        comment=comment if isinstance(comment, str) else None,
    )


def _total_pages(body: Mapping[str, Any]) -> int:
    info = body.get("result_info")
    if isinstance(info, Mapping) and isinstance(info.get("total_pages"), int):
        return max(1, int(info["total_pages"]))
    return 1


class CloudflareClient:
    """Use as ``async with CloudflareClient(token) as cf: ...``."""

    def __init__(
        self,
        token: SecretValue,
        *,
        base_url: str = API_BASE,
        timeout: httpx.Timeout = TIMEOUT,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"CloudflareClient(base_url={self._base_url!r})"

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._http is None:
            raise RuntimeError("CloudflareClient used outside 'async with'")
        response: httpx.Response | None = None
        try:
            response = await self._http.request(
                method,
                path,
                params=dict(params) if params else None,
                json=dict(json) if json is not None else None,
                headers={"Authorization": f"Bearer {self._token.reveal()}"},
            )
        except httpx.HTTPError:
            # httpx exceptions carry the request with its headers: raise outside this block so the
            # new error has no __context__ that a traceback could print.
            pass
        if response is None:
            raise CloudflareError("unavailable")
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise CloudflareError("unavailable" if status >= 500 else "invalid_response", status)
        codes = _codes(body)
        if status == 429:
            raise CloudflareError("rate_limited", status, codes)
        if status >= 500:
            raise CloudflareError("unavailable", status, codes)
        if status in (401, 403):
            raise CloudflareError("denied", status, codes)
        if status == 404:
            raise CloudflareError("not_found", status, codes)
        if status >= 400 or body.get("success") is not True:
            raise CloudflareError("rejected", status, codes)
        return body

    # --- token ----------------------------------------------------------------------------------

    async def verify_user_token(self) -> str:
        """``GET /user/tokens/verify``: the token's status (``active`` when usable)."""
        body = await self._request("GET", "/user/tokens/verify")
        result = body.get("result")
        return str(result.get("status") or "") if isinstance(result, Mapping) else ""

    async def verify_account_token(self, account_id: str) -> str:
        """``GET /accounts/{id}/tokens/verify`` for account-owned tokens."""
        if not HEX32.fullmatch(account_id):
            raise CloudflareError("invalid_response")
        body = await self._request("GET", f"/accounts/{account_id}/tokens/verify")
        result = body.get("result")
        return str(result.get("status") or "") if isinstance(result, Mapping) else ""

    # --- zones and records ------------------------------------------------------------------------

    async def list_zones(self) -> list[Zone]:
        zones: list[Zone] = []
        page, pages = 1, 1
        while page <= pages:
            body = await self._request("GET", "/zones", params={"per_page": ZONES_PER_PAGE, "page": page})
            result = body.get("result")
            if not isinstance(result, list):
                raise CloudflareError("invalid_response")
            zones.extend(parse_zone(z) for z in result if isinstance(z, Mapping))
            pages = min(_total_pages(body), MAX_PAGES)
            page += 1
        return zones

    async def list_dns_records(self, zone_id: str, *, first_page_only: bool = False) -> list[DnsRecord]:
        """Every DNS record of the zone (all types, ours and everybody else's)."""
        self._check_id(zone_id)
        records: list[DnsRecord] = []
        page, pages = 1, 1
        while page <= pages:
            body = await self._request(
                "GET", f"/zones/{zone_id}/dns_records", params={"per_page": RECORDS_PER_PAGE, "page": page}
            )
            result = body.get("result")
            if not isinstance(result, list):
                raise CloudflareError("invalid_response")
            records.extend(parse_record(zone_id, r) for r in result if isinstance(r, Mapping))
            total = _total_pages(body)
            if first_page_only:
                break
            if total > MAX_PAGES:
                # A partial list could make the planner believe one of our records is gone.
                raise CloudflareError("invalid_response")
            pages = total
            page += 1
        return records

    async def can_edit_dns(self, zone_id: str) -> bool:
        """DNS:Edit check that cannot change anything: an empty PATCH of a record id that does not
        exist (32 random hex digits). With the permission Cloudflare answers "not found" (or another
        non-auth 4xx); without it, 401/403 before it looks the record up."""
        self._check_id(zone_id)
        missing_id = secrets.token_hex(16)
        try:
            await self._request("PATCH", f"/zones/{zone_id}/dns_records/{missing_id}", json={})
        except CloudflareError as exc:
            if exc.kind == "denied":
                return False
            if exc.kind in ("not_found", "rejected"):
                return True
            raise
        # A 2xx for a random id is impossible; refuse to draw a conclusion from it.
        raise CloudflareError("invalid_response")

    async def create_record(self, zone_id: str, spec: RecordSpec) -> DnsRecord:
        self._check_id(zone_id)
        body = await self._request("POST", f"/zones/{zone_id}/dns_records", json=spec.body())
        return self._single(zone_id, body)

    async def update_record(self, zone_id: str, record_id: str, spec: RecordSpec) -> DnsRecord:
        self._check_id(zone_id)
        self._check_id(record_id)
        body = await self._request("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", json=spec.body())
        return self._single(zone_id, body)

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        self._check_id(zone_id)
        self._check_id(record_id)
        await self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    @staticmethod
    def _check_id(value: str) -> None:
        if not HEX32.fullmatch(value):
            raise ValueError("not a Cloudflare id")

    @staticmethod
    def _single(zone_id: str, body: Mapping[str, Any]) -> DnsRecord:
        result = body.get("result")
        if not isinstance(result, Mapping):
            raise CloudflareError("invalid_response")
        return parse_record(zone_id, result)
