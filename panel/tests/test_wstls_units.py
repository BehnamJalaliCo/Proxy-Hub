"""VLESS + WebSocket + TLS (transport ws_tls, 2026-11-05): the pure parts.

The link (the shape of the owner's own working config), node names, the end decision per
transport, the agent version gate, the ACME bookkeeping (backoff, renewal), the managed-record plan
for ``_acme-challenge`` records, and the DNS wire format of the DoH check. Certificates are made at
run time; nothing key-shaped sits in the repository.
"""

from __future__ import annotations

import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from acme import messages
from pydantic import ValidationError
from test_wstls_support import make_cert

from proxyhub.acme.doh import DnsWireError, encode_query, parse_txt_response
from proxyhub.acme.protocol import AcmeError, _code, leaf_facts, new_private_key_pem
from proxyhub.acme.store import CertRow, backoff, renewal_due, valid_now
from proxyhub.cloudflare.client import DnsRecord
from proxyhub.cloudflare.records import Desired, OwnedRow, plan_zone
from proxyhub.cloudflare.service import NodeNames, node_dns_name, slot_name, slotless_name
from proxyhub.reality.keys import new_ws_path
from proxyhub.reality.links import config_label, config_name, vless_ws_url
from proxyhub.reality.policy import ActiveConfig, end_status
from proxyhub.reality.settings import GB, RealitySettings
from proxyhub.reality.store import agent_supports_ws_tls

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
UID = uuid.UUID("6a1f3a2e-1111-4c3b-9a55-0123456789ab")


def test_the_link_has_the_shape_of_the_owners_working_config() -> None:
    url = vless_ws_url(
        user_id=UID, host="slot3.ipoxxo.example", path="/0123456789abcdef", name="@ipoxxo 🇩🇪 12"
    )
    assert url == (
        "vless://6a1f3a2e-1111-4c3b-9a55-0123456789ab@slot3.ipoxxo.example:443?encryption=none"
        "&security=tls&sni=slot3.ipoxxo.example&alpn=http%2F1.1&fp=chrome&type=ws&host=slot3.ipoxxo.example"
        "&path=%2F0123456789abcdef#%40ipoxxo%20%F0%9F%87%A9%F0%9F%87%AA%2012"
    )
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    # The owner's link: vless://<uuid>@ir.<domain>:443?path=%2F&security=tls&alpn=http%2F1.1
    # &encryption=none&fp=chrome&type=ws (ours adds sni and host, both the node's own name).
    assert {k: q[k] for k in ("security", "alpn", "encryption", "fp", "type")} == {
        "security": "tls",
        "alpn": "http/1.1",
        "encryption": "none",
        "fp": "chrome",
        "type": "ws",
    }
    assert q["sni"] == q["host"] == parts.hostname == "slot3.ipoxxo.example" and parts.port == 443
    assert q["path"] == "/0123456789abcdef" and "flow" not in q and "pbk" not in q
    assert unquote(parts.fragment) == "@ipoxxo 🇩🇪 12"
    assert config_name("@ipoxxo", "fsn1", 12) == "@ipoxxo 🇩🇪 12"  # D41: the ad in every name
    assert config_label(12) == "کانفیگ ۱۲"


def test_ws_paths_and_node_names() -> None:
    a, b = new_ws_path(), new_ws_path()
    assert a != b and len(a) == 17 and a[0] == "/" and int(a[1:], 16) >= 0
    node = uuid.UUID("0a1b2c3d-9999-4000-8000-000000000000")
    assert slotless_name(node, "ipoxxo.example") == "t0a1b2c3d.ipoxxo.example"
    assert node_dns_name(None, node, "ipoxxo.example") == "t0a1b2c3d.ipoxxo.example"
    assert (
        node_dns_name(4, node, "ipoxxo.example") == slot_name(4, "ipoxxo.example") == "slot4.ipoxxo.example"
    )
    other = uuid.uuid4()
    names = NodeNames(
        zone="ipoxxo.example",
        records={
            "slot4.ipoxxo.example": ("203.0.113.4", other),  # moved to the slot's new server
            "t0a1b2c3d.ipoxxo.example": ("203.0.113.9", node),
        },
    )
    # The old server of slot 4 still serves the slot's name, but the name no longer leads to it.
    assert names.scope_name(4, node) == "slot4.ipoxxo.example"
    assert names.own_name(4, node, "203.0.113.7") is None
    assert names.own_name(4, other, "203.0.113.4") == "slot4.ipoxxo.example"
    assert names.own_name(None, node, "203.0.113.9") == "t0a1b2c3d.ipoxxo.example"
    assert names.scope_name(5, node) is None and names.scope_name(None, other) is None
    assert NodeNames(zone=None).scope_name(4, node) is None


def test_settings_default_to_ws_tls() -> None:
    assert RealitySettings().transport == "ws_tls"
    assert RealitySettings(transport="reality").transport == "reality"
    with pytest.raises(ValidationError):
        RealitySettings.model_validate({"transport": "grpc"})


def test_agent_version_gate() -> None:
    for version, ok in (
        ("0.3.0", True),
        ("0.3.1", True),
        ("0.10.0", True),
        ("1.0.0", True),
        ("0.3.0-rc1", True),
        ("0.2.0", False),
        ("0.2.9", False),
        ("0.1.0", False),
        ("dev", False),
        ("", False),
        (None, False),
    ):
        assert agent_supports_ws_tls(version) is ok, version


def _config(**over: Any) -> ActiveConfig:
    base: dict[str, Any] = {
        "id": 7,
        "node_id": uuid.UUID("0a1b2c3d-9999-4000-8000-000000000000"),
        "slot": 3,
        "key_id": 1,
        "host": "slot3.example.com",
        "created_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=47),
        "data_cap_bytes": 1000 * GB,
        "used_bytes": 10,
        "transport": "ws_tls",
    }
    base.update(over)
    return ActiveConfig(**base)


def test_end_status_for_ws_tls() -> None:
    def why(c: ActiveConfig, *, domain: str | None = "example.com", transport: Any = "ws_tls") -> Any:
        return end_status(
            c, now=NOW, own_node_state="ready", scope_served=True, active_domain=domain, transport=transport
        )

    assert why(_config()) is None
    # A test node's own name works like a slot's: it ends when the domain is no longer the active one.
    test = _config(slot=None, host="t0a1b2c3d.example.com")
    assert why(test) is None
    assert why(test, domain="other.example") == "burned"
    assert why(_config(slot=None, host="t99999999.example.com")) == "burned"  # another node's name
    assert why(_config(), domain="other.example") == "burned"
    # The nodes serve REALITY now: ws_tls links stop working, and the other way round.
    assert why(_config(), transport="reality") == "burned"
    assert why(_config(transport="reality"), transport="ws_tls") == "burned"
    # A ws_tls link never names an IP (no certificate for it): such a row cannot work.
    assert why(_config(slot=None, host="203.0.113.10")) == "burned"
    assert why(_config(used_bytes=1000 * GB)) == "full" and why(_config(expires_at=NOW)) == "expired"


def test_certificate_bookkeeping() -> None:
    assert [backoff(n) for n in range(1, 8)] == [timedelta(hours=h) for h in (1, 2, 4, 8, 16, 24, 24)]

    def row(not_after: datetime | None) -> CertRow:
        return CertRow(
            "ipoxxo.example",
            None,
            None if not_after is None else not_after - timedelta(days=90),
            not_after,
            None,
            None,
            None,
            None,
            0,
            None,
            None,
        )

    assert renewal_due(None, NOW) and renewal_due(row(None), NOW)
    assert renewal_due(row(NOW + timedelta(days=29, hours=23)), NOW)
    assert not renewal_due(row(NOW + timedelta(days=30, minutes=1)), NOW)
    assert valid_now(row(NOW + timedelta(days=2)), NOW)
    assert not valid_now(row(NOW + timedelta(minutes=30)), NOW)  # too close to its end to hand out
    assert not valid_now(row(None), NOW) and not valid_now(None, NOW)


def test_certificate_facts_and_keys() -> None:
    cert = make_cert("*.ipoxxo.example")
    not_before, not_after, serial, fp = leaf_facts(cert.chain_pem)
    assert not_after - not_before == timedelta(days=90) and len(fp) == 64 and int(serial, 16) > 0
    key = new_private_key_pem()
    assert "PRIVATE KEY" in key.reveal() and "PRIVATE" not in repr(key) and "PRIVATE" not in str(key)
    # The CA's problem text never travels: only a short code (rate limits keep their type).
    limited = messages.Error(typ="urn:ietf:params:acme:error:rateLimited", detail="too many: example.com")
    assert _code(limited).code == "acme_rateLimited" and "example" not in str(_code(limited))
    assert _code(ValueError("x")).code == "acme_unexpected"
    assert AcmeError("x" * 100).code == "x" * 60


def _record(rid: str, name: str, rtype: str, content: str, comment: str | None) -> DnsRecord:
    return DnsRecord(rid, "z" * 32, name, rtype, content, 60, False, comment)


def test_acme_records_follow_the_managed_record_rule() -> None:
    zone = "z" * 32
    want = [
        Desired(name="_acme-challenge.ipoxxo.example", type="TXT", content="v1", purpose="acme"),
    ]
    # Nothing there: create. A record nobody manages under the name: a conflict, never touched.
    [create] = plan_zone(zone, want, [], [])
    assert create.op == "create" and create.desired is not None and create.desired.purpose == "acme"
    foreign = _record("a" * 32, "_acme-challenge.ipoxxo.example", "TXT", '"other"', None)
    assert [c.op for c in plan_zone(zone, want, [foreign], [])] == ["conflict"]
    # Ours (marker AND row): changed to the new value, and deleted when nothing is wanted.
    ref = uuid.uuid4()
    ours = _record(
        "b" * 32, "_acme-challenge.ipoxxo.example", "TXT", '"v0"', f"managed-by=proxy-hub ref={ref}"
    )
    row = OwnedRow(ref, zone, "b" * 32, "_acme-challenge.ipoxxo.example", "TXT", '"v0"', 60, False)
    assert [c.op for c in plan_zone(zone, want, [ours], [row])] == ["update"]
    assert [c.op for c in plan_zone(zone, [], [ours], [row])] == ["delete"]
    # The same value with Cloudflare's quotes is no change.
    same = _record(
        "b" * 32, "_acme-challenge.ipoxxo.example", "TXT", '"v1"', f"managed-by=proxy-hub ref={ref}"
    )
    row1 = OwnedRow(ref, zone, "b" * 32, "_acme-challenge.ipoxxo.example", "TXT", '"v1"', 60, False)
    assert plan_zone(zone, want, [same], [row1]) == []
    # A slot record of ours is not an acme row: the acme plan only reads it (no delete).
    slot = _record(
        "c" * 32, "slot1.ipoxxo.example", "A", "203.0.113.1", f"managed-by=proxy-hub ref={uuid.uuid4()}"
    )
    assert [c.op for c in plan_zone(zone, [], [slot], [])] == []


def _response(name: str, answers: list[bytes], rcode: int = 0) -> bytes:
    """A DNS response as a resolver sends it: question, then answers whose owner is a compression
    pointer to the question name (offset 12)."""
    q = encode_query(name)[12:]
    head = struct.pack("!HHHHHH", 0, 0x8180 | rcode, 1, len(answers), 0, 0)
    body = b""
    for rdata in answers:
        body += b"\xc0\x0c" + struct.pack("!HHIH", 16, 1, 60, len(rdata)) + rdata
    return head + q + body


def test_dns_wire_format() -> None:
    q = encode_query("_acme-challenge.ipoxxo.example")
    assert q[:12] == struct.pack("!HHHHHH", 0, 0x0100, 1, 0, 0, 0)
    assert q[12:] == b"\x0f_acme-challenge\x06ipoxxo\x07example\x00\x00\x10\x00\x01"
    value = "tz4yK1Ok7fJ0x-8gOT2gw2ZpYRKkNmJ0pCC9H1n3wGc"
    one = _response("_acme-challenge.ipoxxo.example", [bytes([len(value)]) + value.encode()])
    assert parse_txt_response(one).values == frozenset({value}) and parse_txt_response(one).rcode == 0
    # A record split into strings is joined; two records are two values.
    split = bytes([10]) + value[:10].encode() + bytes([len(value) - 10]) + value[10:].encode()
    two = _response("_acme-challenge.ipoxxo.example", [split, b"\x03old"])
    assert parse_txt_response(two).values == frozenset({value, "old"})
    nx = _response("_acme-challenge.ipoxxo.example", [], rcode=3)
    assert parse_txt_response(nx).rcode == 3 and parse_txt_response(nx).values == frozenset()
    for bad in (b"", one[:20], one[:-5], q):  # short, cut in the question or the rdata, not a response
        with pytest.raises(DnsWireError):
            parse_txt_response(bad)
    with pytest.raises(ValueError):
        encode_query("a" * 64 + ".example")
