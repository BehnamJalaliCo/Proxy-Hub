"""Shared data and helpers for the net team's tests (Cloudflare DNS, probes from Iran).

No imitation of Cloudflare, Globalping or check-host anywhere (decision D10). The JSON below is
DATA in the exact shapes those services return, fed to our real parsers and decision functions:

* Cloudflare zone and DNS record objects follow the documented v4 shape (``zones_zone``,
  ``dns-records_dns-record-response`` in github.com/cloudflare/api-schemas, read 2026-11-03).
  ``CF_INVALID_TOKEN_*`` are the bodies api.cloudflare.com really returned to this sandbox on
  2026-11-03 for a token-shaped random value.
* ``GP_*`` are real Globalping measurements made from this sandbox on 2026-11-03 21:08-21:09 UTC
  (ids in the dicts), trimmed to the fields our parser reads plus ``rawOutput``.
* ``CH_*`` are real check-host.net answers from the same time.

Node IPs in this file are documentation addresses (RFC 5737) or public anycast/service addresses
that appeared as measurement targets; none is an end-user address (CLAUDE.md rule 7).
"""

from __future__ import annotations

import copy
import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ZONE_ID = "023e105f4ecef8ad9ca31a8372d0c353"
ZONE_ID_2 = "9a7806061c88ada191ed06f989cc3dac"
ACCOUNT_ID = "372e67954025e0ba6aaa6d586b9e0b59"


def cf_zone_json(zone_id: str = ZONE_ID, name: str = "example.com", status: str = "active") -> dict[str, Any]:
    return {
        "id": zone_id,
        "name": name,
        "status": status,
        "paused": False,
        "type": "full",
        "development_mode": 0,
        "name_servers": ["bob.ns.cloudflare.com", "lola.ns.cloudflare.com"],
        "original_name_servers": None,
        "original_registrar": None,
        "original_dnshost": None,
        "modified_on": "2026-10-30T10:00:00.12345Z",
        "created_on": "2026-10-30T09:00:00.12345Z",
        "activated_on": "2026-10-30T10:00:00.12345Z",
        "meta": {"step": 2, "custom_certificate_quota": 0, "page_rule_quota": 3, "phishing_detected": False},
        "owner": {"id": None, "type": "user", "email": None},
        "account": {"id": ACCOUNT_ID, "name": "Owner's Account"},
        "tenant": {"id": None, "name": None},
        "tenant_unit": {"id": None},
        "permissions": ["#dns_records:edit", "#dns_records:read", "#zone:read"],
        "plan": {
            "id": "0feeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "name": "Free Website",
            "price": 0,
            "currency": "USD",
        },
    }


def cf_record_json(
    name: str,
    content: str,
    *,
    record_id: str | None = None,
    rtype: str = "A",
    ttl: int = 60,
    proxied: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": record_id or secrets.token_hex(16),
        "name": name,
        "type": rtype,
        "content": content,
        "proxiable": rtype in ("A", "AAAA", "CNAME"),
        "proxied": proxied,
        "ttl": ttl,
        "settings": {},
        "meta": {},
        "comment": comment,
        "tags": [],
        "created_on": "2026-11-01T05:20:00.12345Z",
        "modified_on": "2026-11-01T05:20:00.12345Z",
    }
    if comment is not None:
        body["comment_modified_on"] = "2026-11-01T05:20:00.12345Z"
    return body


# Real bodies from api.cloudflare.com (2026-11-03) for a token-shaped random value.
CF_INVALID_TOKEN_VERIFY = {
    "success": False,
    "errors": [{"code": 1000, "message": "Invalid API Token"}],
    "messages": [],
    "result": None,
}
CF_INVALID_TOKEN_ZONES = {
    "success": False,
    "errors": [{"code": 9109, "message": "Invalid access token"}],
    "messages": [],
    "result": None,
}

# --- Globalping (real measurements, 2026-11-03) -------------------------------------------------

_MCI_PROBE = {
    "continent": "AS",
    "region": "Southern Asia",
    "country": "IR",
    "state": None,
    "city": "Tabriz",
    "asn": 197207,
    "longitude": 46.29,
    "latitude": 38.08,
    "network": "Mobile Communication Company of Iran",
    "tags": ["eyeball-network", "u-Albatrossvpn"],
    "resolvers": ["private"],
}
_RIGHTEL_PROBE = {
    "continent": "AS",
    "region": "Southern Asia",
    "country": "IR",
    "state": None,
    "city": "Tehran",
    "asn": 57218,
    "longitude": 51.42,
    "latitude": 35.69,
    "network": "Rightel Communication Service Company PJS",
    "tags": ["eyeball-network", "u-sudo-user-un:rightel"],
    "resolvers": ["private"],
}
_ARVAN_PROBE = {
    "continent": "AS",
    "region": "Southern Asia",
    "country": "IR",
    "state": None,
    "city": "Tehran",
    "asn": 202468,
    "longitude": 51.42,
    "latitude": 35.69,
    "network": "AbrArvan CDN and IaaS",
    "tags": ["datacenter-network", "u-hatamiarash7"],
    "resolvers": ["private"],
}

# TCP "ping" to www.hetzner.com:443 requested from AS197207, AS57218, AS44244, AS58224: only MCI
# and Rightel had probes; the MCI probe failed internally, Rightel connected 3 of 3 times.
GP_TCP_HETZNER = {
    "id": "2sAWRLjWFTTMzEwt600021REC",
    "type": "ping",
    "status": "finished",
    "createdAt": "2026-11-03T21:08:10.499Z",
    "updatedAt": "2026-11-03T21:08:29.782Z",
    "target": "www.hetzner.com",
    "probesCount": 2,
    "locations": [{"asn": 197207}, {"asn": 57218}, {"asn": 44244}, {"asn": 58224}],
    "measurementOptions": {"protocol": "TCP", "port": 443},
    "results": [
        {
            "probe": _MCI_PROBE,
            "result": {
                "status": "failed",
                "rawOutput": "The measurement timed out.",
                "failureSource": "internal",
            },
        },
        {
            "probe": _RIGHTEL_PROBE,
            "result": {
                "status": "finished",
                "rawOutput": "PING www.hetzner.com (213.133.116.44) on port 443.\n...",
                "resolvedAddress": "213.133.116.44",
                "resolvedHostname": "www.hetzner.com",
                "timings": [{"rtt": 112}, {"rtt": 122}, {"rtt": 91.4}],
                "stats": {
                    "min": 91.42,
                    "max": 122.5,
                    "avg": 108.8,
                    "total": 3,
                    "loss": 0,
                    "rcv": 3,
                    "drop": 0,
                },
            },
        },
    ],
}

# TCP "ping" to 1.1.1.1:443 from the same ASN list: both probes connected.
GP_TCP_ONE = {
    "id": "2QB4z1sfC84imQBjH00021REC",
    "type": "ping",
    "status": "finished",
    "createdAt": "2026-11-03T21:08:09.000Z",
    "updatedAt": "2026-11-03T21:08:24.000Z",
    "target": "1.1.1.1",
    "probesCount": 2,
    "locations": [{"asn": 197207}, {"asn": 57218}, {"asn": 44244}, {"asn": 58224}],
    "measurementOptions": {"protocol": "TCP", "port": 443},
    "results": [
        {
            "probe": _MCI_PROBE,
            "result": {
                "status": "finished",
                "rawOutput": "PING 1.1.1.1 (1.1.1.1) on port 443.\n...",
                "resolvedAddress": "1.1.1.1",
                "resolvedHostname": "one.one.one.one",
                "timings": [{"rtt": 113.3}, {"rtt": 139.3}, {"rtt": 123.6}],
                "stats": {
                    "min": 113.3,
                    "max": 139.3,
                    "avg": 125.4,
                    "total": 3,
                    "loss": 0,
                    "rcv": 3,
                    "drop": 0,
                },
            },
        },
        {
            "probe": _RIGHTEL_PROBE,
            "result": {
                "status": "finished",
                "rawOutput": "PING 1.1.1.1 (1.1.1.1) on port 443.\n...",
                "resolvedAddress": "1.1.1.1",
                "resolvedHostname": "one.one.one.one",
                "timings": [{"rtt": 116.7}, {"rtt": 142.9}, {"rtt": 119.9}],
                "stats": {
                    "min": 116.7,
                    "max": 142.9,
                    "avg": 126.5,
                    "total": 3,
                    "loss": 0,
                    "rcv": 3,
                    "drop": 0,
                },
            },
        },
    ],
}

# TCP "ping" to 1.1.1.1:9 (nothing listens) from Rightel: finished, every attempt lost.
GP_TCP_CLOSED_PORT = {
    "id": "2ePUREFE7SfW8v20C00021RED",
    "type": "ping",
    "status": "finished",
    "createdAt": "2026-11-03T21:09:09.220Z",
    "updatedAt": "2026-11-03T21:09:23.364Z",
    "target": "1.1.1.1",
    "probesCount": 1,
    "locations": [{"asn": 57218}],
    "measurementOptions": {"protocol": "TCP", "port": 9},
    "results": [
        {
            "probe": _RIGHTEL_PROBE,
            "result": {
                "status": "finished",
                "rawOutput": "PING one.one.one.one (1.1.1.1) on port 9.\nNo reply ...",
                "resolvedAddress": "1.1.1.1",
                "resolvedHostname": "one.one.one.one",
                "timings": [],
                "stats": {
                    "min": None,
                    "max": None,
                    "avg": None,
                    "total": 3,
                    "loss": 100,
                    "rcv": 0,
                    "drop": 3,
                },
            },
        }
    ],
}

# DNS A query for youtube.com: the Iranian filter's answer in 10.10.34.0/24 (docs/10 section 3.10).
GP_DNS_HIJACKED = {
    "id": "2ee4l7A5xMGF1RH7O00021REC",
    "type": "dns",
    "status": "finished",
    "createdAt": "2026-11-03T21:08:40.000Z",
    "updatedAt": "2026-11-03T21:08:45.000Z",
    "target": "youtube.com",
    "probesCount": 2,
    "locations": [{"asn": 197207}, {"asn": 57218}, {"asn": 202468}],
    "measurementOptions": {"query": {"type": "A"}},
    "results": [
        {
            "probe": _RIGHTEL_PROBE,
            "result": {
                "status": "finished",
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "rawOutput": "...",
                "resolver": "private",
                "answers": [
                    {"name": "youtube.com.", "type": "A", "ttl": 1, "class": "IN", "value": "10.10.34.35"}
                ],
                "timings": {"total": 31},
            },
        },
        {
            "probe": _ARVAN_PROBE,
            "result": {
                "status": "finished",
                "statusCode": 0,
                "statusCodeName": "NOERROR",
                "rawOutput": "...",
                "resolver": "private",
                "answers": [
                    {"name": "youtube.com.", "type": "A", "ttl": 418, "class": "IN", "value": "10.10.34.36"}
                ],
                "timings": {"total": 12},
            },
        },
    ],
}

GP_NO_PROBES_422 = {
    "error": {"type": "no_probes_found", "message": "No matching IPv4 probes available."},
    "links": {"documentation": "https://globalping.io/docs/api.globalping.io#post-/v1/measurements"},
}

# --- check-host.net (real answers, 2026-11-03) --------------------------------------------------

CH_NODES = {
    "nodes": {
        "ir1.node.check-host.net": {
            "asn": "AS47430",
            "ip": "185.105.238.209",
            "location": ["ir", "Iran", "Tehran"],
        },
        "ir2.node.check-host.net": {
            "asn": "AS209279",
            "ip": "195.182.38.164",
            "location": ["ir", "Iran", "Isfahan"],
        },
        "ir3.node.check-host.net": {
            "asn": "AS213953",
            "ip": "77.104.108.3",
            "location": ["ir", "Iran", "Shiraz"],
        },
        "at1.node.check-host.net": {
            "asn": "AS64457",
            "ip": "185.224.3.111",
            "location": ["at", "Austria", "Vienna"],
        },
    }
}
CH_TCP_CREATED = {
    "nodes": {
        "ir1.node.check-host.net": ["ir", "Iran", "Tehran", "185.105.238.209", "AS47430"],
        "ir2.node.check-host.net": ["ir", "Iran", "Isfahan", "195.182.38.164", "AS209279"],
    },
    "ok": 1,
    "permanent_link": "https://check-host.net/check-report/555dc965k7ff",
    "request_id": "555dc965k7ff",
}
# 1.1.1.1:443 from four Iranian nodes (all connected).
CH_TCP_RESULT_OK = {
    "ir1.node.check-host.net": [{"address": "1.1.1.1", "time": 0.087255}],
    "ir2.node.check-host.net": [{"address": "1.1.1.1", "time": 0.079946}],
    "ir3.node.check-host.net": [{"address": "1.1.1.1", "time": 0.100743}],
    "ir5.node.check-host.net": [{"address": "1.1.1.1", "time": 0.082222}],
}
# 1.1.1.1:9 from two Iranian nodes (nothing listens).
CH_TCP_RESULT_FAIL = {
    "ir1.node.check-host.net": [{"error": "Connection timed out"}],
    "ir2.node.check-host.net": [{"error": "Connection timed out"}],
}
# While a node has not answered yet, check-host reports null for it.
CH_TCP_RESULT_PENDING = {
    "ir1.node.check-host.net": None,
    "ir2.node.check-host.net": [{"error": "Connection timed out"}],
}


def deep(value: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(value)


# --- database helpers -------------------------------------------------------------------------


async def clean_net_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM probe_cycles"))  # measurements, results, rounds cascade
        await conn.execute(text("DELETE FROM probe_node_state"))
        await conn.execute(text("DELETE FROM probe_sources"))
        await conn.execute(text("DELETE FROM cf_records"))
        await conn.execute(text("DELETE FROM cf_zones"))
        await conn.execute(text("DELETE FROM cf_state"))
        await conn.execute(text("DELETE FROM secrets WHERE name = 'cloudflare:token'"))
        await conn.execute(text("DELETE FROM settings WHERE key = 'probes'"))
        await conn.execute(text("DELETE FROM actions_log"))
        await conn.execute(
            text(
                "DELETE FROM telemt_node_configs "
                "WHERE node_id IN (SELECT id FROM nodes WHERE name LIKE 'net-test-%')"
            )
        )
        await conn.execute(text("DELETE FROM nodes WHERE name LIKE 'net-test-%'"))
        await conn.execute(text("DELETE FROM hetzner_accounts WHERE name LIKE 'net-test-%'"))


async def make_node(
    engine: AsyncEngine,
    *,
    state: str = "ready",
    slot: int | None = None,
    ipv4: str | None = "203.0.113.10",
    ready_at: datetime | None = None,
) -> uuid.UUID:
    node_id = uuid.uuid4()
    account_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, 'net-test-token-name', 'active')"
            ),
            {"id": account_id, "name": f"net-test-{account_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, ipv4, "
                "ready_at, is_test) VALUES (:id, :acc, :name, :slot, 'fsn1', 'cpx02', :state, "
                "CAST(:ip AS inet), :ready_at, true)"
            ),
            {
                "id": node_id,
                "acc": account_id,
                "name": f"net-test-{node_id.hex[:8]}",
                "slot": slot,
                "state": state,
                "ip": ipv4,
                "ready_at": ready_at,
            },
        )
    return node_id


async def actions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT actor, action, target, allowed, reasons, result FROM actions_log ORDER BY id")
            )
        ).mappings()
        return [dict(r) for r in rows]
