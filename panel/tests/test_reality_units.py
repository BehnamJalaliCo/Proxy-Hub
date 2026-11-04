"""Reality: pure parts (settings, keys, policy decisions, links, the report schema).

Real cryptography throughout; with PROXYHUB_TEST_XRAY set (node-image/fetch-xray.sh), the pinned
Xray binary itself confirms that the panel derives the public key exactly as Xray does.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from pydantic import ValidationError

from proxyhub.agent_api.reports import ReportRejected, parse_report
from proxyhub.reality.keys import new_key_pair, new_short_id, public_from_private
from proxyhub.reality.links import config_name, flag, vless_url
from proxyhub.reality.policy import ActiveConfig, creation_due, eligible_for_new_configs, end_status
from proxyhub.reality.settings import GB, RealitySettings
from proxyhub.reality.store import NodeView, StatusView, config_email, config_id_from_email, is_ip_host
from proxyhub.reconcilers.reality import target_alert_text

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def test_settings_defaults_follow_the_contract() -> None:
    s = RealitySettings()
    assert (s.enabled, s.configs_per_node, s.cap_hours, s.cap_gb) == (True, 2, 24, 50)
    assert s.cap_bytes == 50 * GB == 50_000_000_000
    assert s.target == "dl.google.com:443" and s.link_name is None
    assert (
        RealitySettings(target_host="  WWW.Example.COM ", link_name="@ipoxxo").target_host
        == "www.example.com"
    )
    assert RealitySettings(link_name="@ipoxxo").link_name == "ipoxxo"
    for bad in (
        {"target_host": "1.2.3.4"},
        {"target_host": "localhost"},
        {"cap_hours": 0},
        {"cap_gb": 0},
        {"configs_per_node": 0},
        {"configs_per_node": 21},
        {"link_name": "<b>x</b>"},
        {"link_name": ""},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            RealitySettings.model_validate(bad)


def test_key_pairs_are_x25519_in_xrays_encoding() -> None:
    pair = new_key_pair()
    private = pair.private.reveal()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", private) and re.fullmatch(r"[A-Za-z0-9_-]{43}", pair.public)
    assert public_from_private(private) == pair.public
    assert private not in repr(pair) and private not in str(pair.private)
    assert len(base64.urlsafe_b64decode(private + "=")) == 32
    assert new_key_pair().public != pair.public
    sid = new_short_id()
    assert re.fullmatch(r"[0-9a-f]{16}", sid) and sid != new_short_id()


@pytest.mark.skipif(not os.environ.get("PROXYHUB_TEST_XRAY"), reason="PROXYHUB_TEST_XRAY not set")
def test_pinned_xray_derives_the_same_public_key() -> None:
    binary = Path(os.environ["PROXYHUB_TEST_XRAY"])
    lock = (REPO / "node-image" / "versions.lock").read_text()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert f"XCORE_BINARY_SHA256={digest}" in lock, "not the pinned Xray build"
    pair = new_key_pair()
    out = subprocess.run(  # noqa: S603 - the pinned binary, hash-checked above
        [str(binary), "x25519", "-i", pair.private.reveal()], capture_output=True, text=True, check=True
    ).stdout
    # Xray 26.10.31 prints "Password (PublicKey): <key>" (docs/10 X4: the label changed over time).
    published = re.search(r"PublicKey\)?:\s*([A-Za-z0-9_-]{43})", out)
    assert published is not None and published.group(1) == pair.public


def _config(**over: Any) -> ActiveConfig:
    base: dict[str, Any] = {
        "id": 7,
        "node_id": uuid.uuid4(),
        "slot": 3,
        "key_id": 1,
        "host": "slot3.example.com",
        "created_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=23),
        "data_cap_bytes": 50 * GB,
        "used_bytes": 10,
    }
    base.update(over)
    return ActiveConfig(**base)


def test_end_status() -> None:
    def why(
        c: ActiveConfig, state: str | None = "ready", served: bool = True, domain: str | None = "example.com"
    ) -> Any:
        return end_status(c, now=NOW, own_node_state=state, scope_served=served, active_domain=domain)

    assert why(_config()) is None
    assert why(_config(used_bytes=50 * GB)) == "full"
    assert why(_config(expires_at=NOW)) == "expired"
    assert why(_config(used_bytes=50 * GB, expires_at=NOW)) == "full"
    assert why(_config(), served=False) == "burned"
    # A slot link survives its own server leaving (the slot's new server serves it) ...
    assert why(_config(), state="burned") is None
    assert why(_config(), state=None) is None
    # ... but not a domain that is no longer the active one.
    assert why(_config(), domain="other.example") == "burned"
    assert why(_config(), domain=None) == "burned"
    # An IP link works only on its own server while that server is in service.
    ip = _config(host="203.0.113.10", slot=None)
    assert why(ip) is None and why(ip, state="unhealthy") is None
    for state in ("burned", "draining", "retiring", "deleted", None):
        assert why(ip, state=state) == "burned", state


def test_creation_is_staggered() -> None:
    s = RealitySettings()  # 2 per scope, 24 h: one every 12 h
    assert creation_due([], now=NOW, settings=s)
    assert not creation_due([NOW - timedelta(hours=11, minutes=59)], now=NOW, settings=s)
    assert creation_due([NOW - timedelta(hours=12)], now=NOW, settings=s)
    assert not creation_due([NOW - timedelta(hours=20), NOW - timedelta(hours=8)], now=NOW, settings=s)
    four = RealitySettings(configs_per_node=4)
    assert creation_due([NOW], now=NOW, settings=four)  # two or more missing: no waiting
    assert not creation_due([NOW, NOW - timedelta(hours=1), NOW], now=NOW, settings=four)
    assert creation_due(
        [NOW - timedelta(hours=6), NOW - timedelta(hours=7), NOW - timedelta(hours=8)], now=NOW, settings=four
    )


def _node(**over: Any) -> NodeView:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "n1",
        "slot": 1,
        "state": "ready",
        "ipv4": "203.0.113.10",
        "location": "fsn1",
        "ready_at": NOW - timedelta(hours=1),
    }
    base.update(over)
    return NodeView(**base)


def _status(node: NodeView, **over: Any) -> StatusView:
    base: dict[str, Any] = {
        "node_id": node.id,
        "enabled": True,
        "up": True,
        "version": "26.10.31",
        "uptime_s": 10.0,
        "target_ok": True,
        "apply_error": None,
        "users_reported": 0,
        "reported_at": NOW - timedelta(minutes=1),
    }
    base.update(over)
    return StatusView(**base)


def test_eligibility_for_new_configs() -> None:
    n = _node()
    assert eligible_for_new_configs(n, _status(n), now=NOW, telemt_clashes=False)
    assert eligible_for_new_configs(n, _status(n, target_ok=None, up=False), now=NOW, telemt_clashes=False)
    refused = {
        "no report": (n, None, False),
        "old agent / no xray": (n, _status(n, enabled=False), False),
        "stale": (n, _status(n, reported_at=NOW - timedelta(minutes=11)), False),
        "xray error": (n, _status(n, apply_error="xray_config_refused"), False),
        "target failed": (n, _status(n, target_ok=False), False),
        "telemt clash": (n, _status(n), True),
        "not ready": (_node(state="unhealthy"), _status(n), False),
        "no ipv4": (_node(ipv4=None), _status(n), False),
    }
    for name, (node, status, clash) in refused.items():
        assert not eligible_for_new_configs(node, status, now=NOW, telemt_clashes=clash), name


def test_labels_and_hosts() -> None:
    assert config_email(42) == "cfg-42@proxyhub" and config_id_from_email("cfg-42@proxyhub") == 42
    for bad in ("cfg-0@proxyhub", "cfg-01@proxyhub", "cfg-1@other", "x@proxyhub", "cfg-1@proxyhub "):
        assert config_id_from_email(bad) is None, bad
    assert is_ip_host("203.0.113.10") and not is_ip_host("slot1.example.com")


def test_vless_link_format() -> None:
    uid = uuid.UUID("6a1f3a2e-1111-4c3b-9a55-0123456789ab")
    name = config_name("ipoxxo", "fsn1", 12)
    assert name == "ipoxxo 🇩🇪 12" and flag("hel1") == "🇫🇮" and flag("xyz9") == "🌐"
    url = vless_url(
        user_id=uid,
        host="slot1.example.com",
        server_name="dl.google.com",
        public_key="8p87hMiKw4fxZrvr8Vy4VsL5AiQAvE1GuFsQO16O2X4",
        short_id="0123456789abcdef",
        name=name,
    )
    assert url.startswith(
        "vless://6a1f3a2e-1111-4c3b-9a55-0123456789ab@slot1.example.com:443?encryption=none&security=reality"
        "&sni=dl.google.com&fp=chrome&pbk=8p87hMiKw4fxZrvr8Vy4VsL5AiQAvE1GuFsQO16O2X4&sid=0123456789abcdef"
        "&type=tcp&flow=xtls-rprx-vision#"
    )
    parts = urlsplit(url)
    assert unquote(parts.fragment) == "ipoxxo 🇩🇪 12" and " " not in url
    assert dict(parse_qsl(parts.query))["pbk"] == "8p87hMiKw4fxZrvr8Vy4VsL5AiQAvE1GuFsQO16O2X4"


def _report(xray: Any = None, with_key: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": 1,
        "agent_version": "0.2.0",
        "applied_etag": None,
        "apply_ok": True,
        "apply_error": None,
        "telemt": {
            "up": False,
            "version": None,
            "uptime_s": None,
            "connections_total": None,
            "connections_bad_total": None,
            "handshake_timeouts_total": None,
            "users": [],
        },
        "system": {
            "uptime_s": None,
            "load1": None,
            "mem_total_bytes": None,
            "mem_available_bytes": None,
            "cpu_count": None,
        },
    }
    if with_key:
        body["xray"] = xray
    return body


GOOD_XRAY = {
    "enabled": True,
    "up": True,
    "version": "26.10.31",
    "uptime_s": 12.0,
    "target_ok": True,
    "apply_error": None,
    "users": [{"email": "cfg-1@proxyhub", "total_octets": 5}, {"email": "cfg-2@proxyhub", "total_octets": 0}],
}


def test_report_xray_section() -> None:
    parsed = parse_report(_report(GOOD_XRAY))
    assert parsed.xray is not None and parsed.xray.users[0].total_octets == 5
    assert parse_report(_report(with_key=False)).xray is None  # an older agent
    assert parse_report(_report(None)).xray is None

    def code(xray: dict[str, Any]) -> str:
        with pytest.raises(ReportRejected) as err:
            parse_report(_report(xray))
        return err.value.code

    assert code({**GOOD_XRAY, "users": [{"email": "cfg-1@10.0.0.1", "total_octets": 1}]}) == "ip_like_value"
    assert code({**GOOD_XRAY, "client_ips": ["198.51.100.7"]}) == "ip_like_value"
    assert code({**GOOD_XRAY, "version": "26.10.31.1"}) == "ip_like_value"  # four parts: address-shaped
    assert code({**GOOD_XRAY, "version": "v26.10"}) == "schema"
    assert code({**GOOD_XRAY, "online": 3}) == "schema"
    assert code({**GOOD_XRAY, "users": [{"email": "a>>>b@x", "total_octets": 1}]}) == "schema"
    assert code({**GOOD_XRAY, "users": [{"email": "cfg-1@proxyhub", "total_octets": -1}]}) == "schema"
    assert (
        code({**GOOD_XRAY, "users": [{"email": "cfg-1@proxyhub", "total_octets": 1, "ip": "x"}]}) == "schema"
    )
    dup = [{"email": "cfg-1@proxyhub", "total_octets": 1}] * 2
    assert code({**GOOD_XRAY, "users": dup}) == "schema"
    assert code({**GOOD_XRAY, "up": "yes"}) == "schema"


def test_target_alert_is_persian_and_escaped() -> None:
    text = target_alert_text(["ph-<b>-1", "ph-2"])
    assert "سایت هدف" in text and "&lt;b&gt;" in text and "۲" in text
