"""DNS-over-HTTPS TXT lookups (RFC 8484, wire format), to see a challenge record live before the CA
is asked to validate it.

Public resolvers (Cloudflare's 1.1.1.1 and Google's 8.8.8.8) are asked over HTTPS, the same way
the Iran filter checks use DoH: no resolver of the panel's host is trusted, and no port 53 is
needed. A name is asked only AFTER its record was created, so no resolver caches a negative answer
for it (a zone's negative TTL can be half an hour). Only our own ``_acme-challenge`` names are
looked up; nothing about any user is involved.
"""

from __future__ import annotations

import asyncio
import ssl
import struct
from dataclasses import dataclass
from typing import Final

import httpx

from proxyhub.agent_api.validation import valid_domain

# Both are RFC 8484 endpoints (POST application/dns-message).
DEFAULT_RESOLVERS: Final = ("https://cloudflare-dns.com/dns-query", "https://dns.google/dns-query")
TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
DNS_MESSAGE: Final = "application/dns-message"
TYPE_TXT: Final = 16
CLASS_IN: Final = 1
MAX_RESPONSE: Final = 64 * 1024


class DnsWireError(ValueError):
    """A DNS message that does not parse (the text is fixed, never response bytes)."""


def encode_query(name: str, qtype: int = TYPE_TXT) -> bytes:
    """One question, recursion desired, id 0 (RFC 8484 section 4.1: cache friendly)."""
    labels = name.rstrip(".").split(".")
    qname = b""
    for label in labels:
        raw = label.encode("ascii")
        if not 1 <= len(raw) <= 63:
            raise ValueError("bad DNS label")
        qname += bytes([len(raw)]) + raw
    return (
        struct.pack("!HHHHHH", 0, 0x0100, 1, 0, 0, 0) + qname + b"\x00" + struct.pack("!HH", qtype, CLASS_IN)
    )


def _skip_name(msg: bytes, pos: int) -> int:
    """The offset right after a (possibly compressed) name that starts at ``pos``."""
    for _ in range(128):
        if pos >= len(msg):
            raise DnsWireError("name runs past the message")
        length = msg[pos]
        if length & 0xC0 == 0xC0:
            if pos + 2 > len(msg):
                raise DnsWireError("cut pointer")
            return pos + 2
        if length & 0xC0:
            raise DnsWireError("bad label type")
        if length == 0:
            return pos + 1
        pos += 1 + length
    raise DnsWireError("name too long")


def _txt_strings(rdata: bytes) -> str:
    out, pos = b"", 0
    while pos < len(rdata):
        n = rdata[pos]
        if pos + 1 + n > len(rdata):
            raise DnsWireError("cut TXT string")
        out += rdata[pos + 1 : pos + 1 + n]
        pos += 1 + n
    return out.decode("ascii", "replace")


@dataclass(frozen=True)
class TxtAnswer:
    rcode: int | None
    values: frozenset[str]
    error: str | None = None


def parse_txt_response(msg: bytes) -> TxtAnswer:
    """RCODE and every TXT value in the answer section (strings of one record joined)."""
    if len(msg) < 12:
        raise DnsWireError("short message")
    _id, flags, qdcount, ancount, _ns, _ar = struct.unpack("!HHHHHH", msg[:12])
    if not flags & 0x8000:
        raise DnsWireError("not a response")
    pos = 12
    for _ in range(qdcount):
        pos = _skip_name(msg, pos) + 4
    values: set[str] = set()
    for _ in range(ancount):
        pos = _skip_name(msg, pos)
        if pos + 10 > len(msg):
            raise DnsWireError("cut record")
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", msg[pos : pos + 10])
        pos += 10
        rdata = msg[pos : pos + rdlength]
        if len(rdata) != rdlength:
            raise DnsWireError("cut rdata")
        pos += rdlength
        if rtype == TYPE_TXT:
            values.add(_txt_strings(rdata))
    return TxtAnswer(rcode=flags & 0x000F, values=frozenset(values))


class DohResolver:
    """``async with DohResolver(url) as r: await r.txt(name)``. ``verify`` is for a test resolver's
    own CA (the public resolvers use the system roots)."""

    def __init__(
        self, url: str, *, verify: ssl.SSLContext | bool = True, timeout: httpx.Timeout = TIMEOUT
    ) -> None:
        self.url = url
        self._client = httpx.AsyncClient(
            timeout=timeout,
            verify=verify,
            headers={"accept": DNS_MESSAGE, "content-type": DNS_MESSAGE, "user-agent": "proxy-hub-panel"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> DohResolver:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def txt(self, name: str) -> TxtAnswer:
        prefix = "_acme-challenge."
        if not name.startswith(prefix) or not valid_domain(name[len(prefix) :]):
            return TxtAnswer(None, frozenset(), error="invalid_name")
        try:
            resp = await self._client.post(self.url, content=encode_query(name))
        except httpx.TimeoutException:
            return TxtAnswer(None, frozenset(), error="timeout")
        except httpx.HTTPError:
            return TxtAnswer(None, frozenset(), error="unreachable")
        if resp.status_code != 200:
            return TxtAnswer(None, frozenset(), error=f"http_{resp.status_code}")
        if len(resp.content) > MAX_RESPONSE:
            return TxtAnswer(None, frozenset(), error="too_large")
        try:
            return parse_txt_response(resp.content)
        except DnsWireError:
            return TxtAnswer(None, frozenset(), error="invalid_response")


async def wait_for_txt(
    resolvers: list[DohResolver],
    name: str,
    value: str,
    *,
    timeout_s: float = 300.0,
    interval_s: float = 5.0,
) -> bool:
    """True once EVERY resolver answers ``name`` with ``value`` among its TXT values (other values
    may still be there for a short while); False when ``timeout_s`` passes first."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    pending = list(resolvers)
    while True:
        still: list[DohResolver] = []
        for r in pending:
            answer = await r.txt(name)
            if value not in answer.values:
                still.append(r)
        pending = still
        if not pending:
            return True
        if loop.time() + interval_s > deadline:
            return False
        await asyncio.sleep(interval_s)
