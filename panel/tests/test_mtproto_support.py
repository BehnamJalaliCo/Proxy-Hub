"""Shared helpers for team mtproto's tests (no tests in this module).

Everything is real: PostgreSQL, the cryptography library, the agent API's own desired-state and
report code, and (in test_mtproto_telemt_real.py) the Go agent and the pinned Telemt 3.5.16 binary.
Nothing here imitates Telegram, Cloudflare or Hetzner (D10): Cloudflare state is the panel's own
table rows, as the DNS loop writes them after Cloudflare accepted a record. Node IPv4s are
documentation addresses (RFC 5737), never an end-user address (rule 7).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import struct
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from proxyhub.agent_api.desired import build_desired_state, canonical_json, etag_for
from proxyhub.agent_api.reports import parse_report, store_report

PREFIX = "mtp-test-"
ZONE_ID = "5f0c1a2b3c4d5e6f708192a3b4c5d6e7"
ZONE_NAME = "ipoxxo-test.example"


async def clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM cf_records"))
        await conn.execute(text("DELETE FROM domain_checks"))
        await conn.execute(text("DELETE FROM cf_zones"))
        await conn.execute(text("DELETE FROM nodes WHERE name LIKE :p"), {"p": f"{PREFIX}%"})
        await conn.execute(text("DELETE FROM hetzner_accounts WHERE name LIKE :p"), {"p": f"{PREFIX}%"})
        await conn.execute(
            text(
                "DELETE FROM mtproxy_stats WHERE registration_id IN "
                "(SELECT id FROM mtproxy_registrations WHERE host LIKE :p)"
            ),
            {"p": "%.test-reg"},
        )
        await conn.execute(text("DELETE FROM mtproxy_registrations WHERE host LIKE :p"), {"p": "%.test-reg"})
        await conn.execute(text("DELETE FROM settings WHERE key = 'mtproto'"))
        await conn.execute(text("DELETE FROM actions_log"))


async def make_node(
    engine: AsyncEngine,
    *,
    state: str = "ready",
    slot: int | None = None,
    ipv4: str | None = "203.0.113.10",
    is_test: bool = False,
    deleted_at: datetime | None = None,
) -> uuid.UUID:
    node_id, account_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, 'mtp-test-token-file-name', 'active')"
            ),
            {"id": account_id, "name": f"{PREFIX}{account_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, ipv4, "
                "is_test, deleted_at) VALUES (:id, :acc, :name, :slot, 'fsn1', 'cpx02', :state, "
                "CAST(:ip AS inet), :test, :deleted_at)"
            ),
            {
                "id": node_id,
                "acc": account_id,
                "name": f"{PREFIX}{node_id.hex[:8]}",
                "slot": slot,
                "state": state,
                "ip": ipv4,
                "test": is_test,
                "deleted_at": deleted_at,
            },
        )
    return node_id


async def set_node_state(engine: AsyncEngine, node_id: uuid.UUID, state: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE nodes SET state = CAST(:s AS varchar), "
                "deleted_at = CASE WHEN CAST(:s AS varchar) = 'deleted' THEN now() END WHERE id = :id"
            ),
            {"s": state, "id": node_id},
        )


async def make_active_zone(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_zones (id, name, status, pool_state, seen_at) "
                "VALUES (:id, :name, 'active', 'active', now())"
            ),
            {"id": ZONE_ID, "name": ZONE_NAME},
        )


async def make_slot_record(
    engine: AsyncEngine, node_id: uuid.UUID, slot: int, ip: str, *, created: bool = True
) -> None:
    """The DNS loop's own row for ``slot<N>.<zone>`` (``cf_record_id`` set once Cloudflare accepted it)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, "
                "purpose, node_id) VALUES (:id, :zone, :cf, :name, 'A', :ip, 60, false, 'slot', :node)"
            ),
            {
                "id": uuid.uuid4(),
                "zone": ZONE_ID,
                "cf": secrets.token_hex(16) if created else None,
                "name": f"slot{slot}.{ZONE_NAME}",
                "ip": ip,
                "node": node_id,
            },
        )


async def register_agent(engine: AsyncEngine, node_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agent_nodes (node_id, agent_version, telemt_version, registered_at, "
                "last_seen_at) VALUES (:n, '0.1.0', '3.5.16', now(), now())"
            ),
            {"n": node_id},
        )


async def current_etag(engine: AsyncEngine, seal_key: bytes, node_id: uuid.UUID) -> str:
    async with async_sessionmaker(engine)() as session:
        return etag_for(canonical_json(await build_desired_state(session, seal_key, node_id)))


async def agent_reports(
    engine: AsyncEngine,
    node_id: uuid.UUID,
    *,
    applied_etag: str | None,
    apply_ok: bool = True,
    telemt_up: bool = True,
    users: list[dict[str, Any]] | None = None,
) -> None:
    """A report as the node agent sends it, through the agent API's own strict parser and store."""
    report = parse_report(
        {
            "format": 1,
            "agent_version": "0.1.0",
            "applied_etag": applied_etag,
            "apply_ok": apply_ok,
            "apply_error": None if apply_ok else "telemt_check_failed",
            "telemt": {
                "up": telemt_up,
                "version": "3.5.16",
                "uptime_s": 12.5,
                "connections_total": 3,
                "connections_bad_total": 0,
                "handshake_timeouts_total": 0,
                "users": users or [],
            },
            "system": {
                "uptime_s": 100.0,
                "load1": 0.1,
                "mem_total_bytes": 2_000_000_000,
                "mem_available_bytes": 1_000_000_000,
                "cpu_count": 2,
            },
        }
    )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await store_report(session, node_id, report, datetime.now(UTC))
        await session.commit()


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def actions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT actor, action, target, allowed, reasons, result FROM actions_log ORDER BY id")
        )
        return [dict(r) for r in result.mappings()]


async def all_table_text(engine: AsyncEngine) -> str:
    """Every row of every table as text (for "this value is stored nowhere" checks)."""
    out: list[str] = []
    async with engine.connect() as conn:
        tables = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        ).scalars()
        for table in list(tables):
            out.extend(
                str(v)
                for v in (await conn.execute(text(f'SELECT CAST(t AS text) FROM "{table}" t'))).scalars()  # noqa: S608
            )
    return "\n".join(out)


def session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# --- fake-TLS (ee) handshake, client side ---------------------------------------------------------
#
# What an official Telegram client does with an ``ee`` link (Telemt 3.5.16 src/protocol/tls.rs,
# validate_tls_handshake / build_server_hello): a TLS 1.3 ClientHello whose 32-byte random is
# HMAC-SHA256(secret, the record with the random zeroed), with the last 4 bytes XORed with the unix
# time; the SNI is the domain carried in the link. Telemt answers a valid one with a ServerHello
# flight whose random is HMAC-SHA256(secret, our random + the flight with that field zeroed). A
# client without the secret gets no such answer. This proves a link's secret and domain against the
# real binary, without Telegram.


def fake_tls_client_hello(secret: bytes, sni: str) -> tuple[bytes, bytes]:
    name = sni.encode("ascii")
    sni_ext = struct.pack("!HHHBH", 0x0000, len(name) + 5, len(name) + 3, 0, len(name)) + name
    groups = struct.pack("!HHHH", 0x000A, 4, 2, 0x001D)
    key_share = struct.pack("!HHHHH", 0x0033, 38, 36, 0x001D, 32) + os.urandom(32)
    versions = struct.pack("!HHBH", 0x002B, 3, 2, 0x0304)
    sigalgs = struct.pack("!HHHHH", 0x000D, 6, 4, 0x0403, 0x0804)
    extensions = sni_ext + groups + key_share + versions + sigalgs
    body = (
        b"\x03\x03"
        + b"\x00" * 32
        + b"\x20"
        + os.urandom(32)
        + struct.pack("!H", 6)
        + b"\x13\x01\x13\x02\x13\x03"
        + b"\x01\x00"
        + struct.pack("!H", len(extensions))
        + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    record = bytearray(b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake)
    mac = hmac.new(secret, bytes(record), hashlib.sha256).digest()
    stamp = struct.pack("<I", int(time.time()))
    digest = mac[:28] + bytes(a ^ b for a, b in zip(mac[28:], stamp, strict=True))
    record[11:43] = digest
    return bytes(record), digest


def fake_tls_handshake_ok(port: int, secret: bytes, sni: str) -> bool:
    """True when the server at 127.0.0.1:``port`` proves it knows ``secret`` for ``sni``."""
    hello, digest = fake_tls_client_hello(secret, sni)
    flight = b""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(hello)
        sock.settimeout(5)
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                flight += chunk
                sock.settimeout(1.0)  # the flight is complete once the server waits for us
        except TimeoutError:
            pass
    if len(flight) < 43 or flight[0] != 0x16:
        return False
    zeroed = flight[:11] + b"\x00" * 32 + flight[43:]
    want = hmac.new(secret, digest + zeroed, hashlib.sha256).digest()
    return hmac.compare_digest(flight[11:43], want)
