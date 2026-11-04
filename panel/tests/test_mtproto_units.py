"""Pure parts of team mtproto: link formats, keys, settings rules, host choice, status.

Link formats are checked against Telegram's own documentation (core.telegram.org/api/links "MTProxy
links", read 2026-11-04: ``t.me/proxy?server=<server>&port=<port>&secret=<secret>``, server = "IP
address or host"; core.telegram.org/mtproto/mtproto-transports "Transport obfuscation": the secret
is 16 bytes in hex, a 17-byte form starts with 0xdd, the doc's own example ``0xdd999...``), and
against Telemt 3.5.16 for fake-TLS (FAQ: ``petrovich.ru`` in HEX is ``706574726f766963682e7275``;
src/api/users/links.rs: ``secret=ee{secret}{hex(domain)}``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import ValidationError

from proxyhub.agent_api.validation import valid_user_name
from proxyhub.mtproto.links import to_link
from proxyhub.mtproto.service import ProxyView, pick_host, proxy_status
from proxyhub.mtproto.settings import (
    DEFAULT_PORT,
    DEFAULT_TLS_DOMAIN,
    MtprotoSettings,
    SettingsCorrupt,
    link_mode,
    parse_settings,
    settings_diff,
)
from proxyhub.mtproto.urls import (
    label_for,
    link_secret,
    parse_key,
    persian_number,
    proxy_key,
    proxy_url,
    user_name_for,
)

SECRET = "0123456789abcdef0123456789abcdef"
# core.telegram.org/mtproto/mtproto-transports, pseudocode:
#   secret := substr(0xdd99999999999999999999999999999999, 1, 16)
DOC_SECRET_17 = "dd99999999999999999999999999999999"
# Telemt 3.5.16 docs/FAQ.en.md: "`petrovich.ru` - `tls` + `masking` host, in HEX: `706574726f766963682e7275`"
PETROVICH_HEX = "706574726f766963682e7275"


def test_secure_secret_is_the_documented_17_byte_form() -> None:
    assert link_secret("9" * 32, "secure", "ignored.example") == DOC_SECRET_17
    assert len(bytes.fromhex(DOC_SECRET_17)) == 17


def test_classic_secret_is_the_plain_16_bytes() -> None:
    assert link_secret(SECRET, "classic", "ignored.example") == SECRET
    assert len(bytes.fromhex(SECRET)) == 16


def test_fake_tls_secret_is_ee_secret_and_domain_hex() -> None:
    s = link_secret(SECRET, "tls", "petrovich.ru")
    assert s == "ee" + SECRET + PETROVICH_HEX
    assert bytes.fromhex(s[34:]).decode() == "petrovich.ru"
    assert link_secret(SECRET, "tls", DEFAULT_TLS_DOMAIN).endswith(DEFAULT_TLS_DOMAIN.encode().hex())


@pytest.mark.parametrize("bad", ["", "0" * 31, "0" * 33, "G" * 32, SECRET.upper(), "ee" + SECRET])
def test_link_secret_refuses_anything_but_32_lower_hex(bad: str) -> None:
    for mode in ("tls", "secure", "classic"):
        with pytest.raises(ValueError):
            link_secret(bad, mode, "x.example")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("host", "mode"),
    [
        ("slot3.ipoxxo.example", "tls"),
        ("203.0.113.7", "tls"),
        ("203.0.113.7", "secure"),
        ("h.example", "classic"),
    ],
)
def test_proxy_url_is_telegrams_t_me_proxy_link(host: str, mode: str) -> None:
    url = proxy_url(host, 8443, SECRET, mode, "petrovich.ru")  # type: ignore[arg-type]
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path, parts.fragment) == ("https", "t.me", "/proxy", "")
    query = parse_qsl(parts.query, strict_parsing=True)
    assert [k for k, _ in query] == ["server", "port", "secret"]
    values = dict(query)
    assert values["server"] == host and values["port"] == "8443"
    assert values["secret"] == link_secret(SECRET, mode, "petrovich.ru")  # type: ignore[arg-type]
    assert "tg://" not in url
    # Nothing in the query needs escaping, so the link reads exactly as Telegram documents it.
    assert url == f"https://t.me/proxy?server={host}&port=8443&secret={values['secret']}"


def test_keys_round_trip_and_reject_foreign_shapes() -> None:
    node = uuid.uuid4()
    user = user_name_for(node)
    key = proxy_key(node, user)
    assert key == f"mtproto:{node}:{user}" and len(key) <= 64
    assert parse_key(key) == (node, user)
    for bad in (
        f"reality:{node}:{user}",
        f"mtproto:{node}",
        f"mtproto:{str(node).upper()}:{user}",
        f"mtproto:{node}:{user}:x",
        f"mtproto:{node}:Bad",
        f" mtproto:{node}:{user}",
    ):
        assert parse_key(bad) is None, bad


def test_user_name_is_a_valid_unique_telemt_user() -> None:
    names = {user_name_for(uuid.uuid4()) for _ in range(500)}
    assert len(names) == 500
    for name in names:
        assert valid_user_name(name) and len(name) == 19 and name.startswith("mt_")


def test_persian_labels() -> None:
    assert persian_number(1093) == "۱۰۹۳"
    assert label_for(1, False) == "پروکسی ۱"
    assert label_for(10, True) == "پروکسی ۱۰"
    assert label_for(None, True) == "پروکسی آزمایشی"
    assert label_for(None, False) == "پروکسی"


# --- settings ------------------------------------------------------------------------------------


def test_settings_defaults_follow_the_contract() -> None:
    s = MtprotoSettings()
    assert (s.port, s.tls_domain, s.mode_tls, s.mode_secure, s.mode_classic) == (
        8443,
        "www.microsoft.com",
        True,
        False,
        False,
    )
    assert DEFAULT_PORT == 8443 and s.link_mode == "tls"


@pytest.mark.parametrize(
    "bad",
    [
        {"port": 443},  # Xray VLESS Reality on every node
        {"port": 22},
        {"port": 9090},
        {"port": 9091},
        {"port": 0},
        {"port": 65536},
        {"port": "8443"},  # strict: no coercion
        {"port": 8443.0},
        {"port": True},
        {"tls_domain": "203.0.113.5"},
        {"tls_domain": "localhost"},
        {"tls_domain": "bad domain.com"},
        {"tls_domain": "a/b.com"},
        {"tls_domain": ""},
        {"mode_tls": False},  # then no mode is on
        {"mode_tls": "yes"},
        {"extra": 1},
    ],
)
def test_settings_refuse(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MtprotoSettings.model_validate({**MtprotoSettings().model_dump(), **bad})


def test_settings_normalise_the_domain() -> None:
    s = MtprotoSettings.model_validate({"tls_domain": " WWW.Apple.COM. "})
    assert s.tls_domain == "www.apple.com"


def test_link_mode_prefers_tls_then_secure_then_classic() -> None:
    assert link_mode(tls=True, secure=True, classic=True) == "tls"
    assert link_mode(tls=False, secure=True, classic=True) == "secure"
    assert link_mode(tls=False, secure=False, classic=True) == "classic"
    with pytest.raises(ValueError):
        link_mode(tls=False, secure=False, classic=False)
    s = MtprotoSettings(mode_tls=False, mode_classic=True)
    assert s.link_mode == "classic"


def test_stored_settings_that_no_longer_validate_are_corrupt() -> None:
    with pytest.raises(SettingsCorrupt):
        parse_settings({"port": 443})
    with pytest.raises(SettingsCorrupt):
        parse_settings("nonsense")


def test_settings_diff() -> None:
    a, b = MtprotoSettings(), MtprotoSettings(port=9443, mode_secure=True)
    assert settings_diff(a, b) == {"port": [8443, 9443], "mode_secure": [False, True]}
    assert settings_diff(None, b) == {"*": ["corrupt", "replaced"]}
    assert settings_diff(a, a) == {}


# --- host and status -------------------------------------------------------------------------------


def test_host_is_the_slot_name_only_when_its_record_leads_to_this_node() -> None:
    zone = "ipoxxo.example"
    rec = {("slot2.ipoxxo.example", "203.0.113.2")}
    assert pick_host(2, "203.0.113.2", zone, rec) == ("slot2.ipoxxo.example", "domain")
    # no active domain / no slot / record for another IP / other slot / no record: the IPv4
    assert pick_host(2, "203.0.113.2", None, rec) == ("203.0.113.2", "ip")
    assert pick_host(None, "203.0.113.2", zone, rec) == ("203.0.113.2", "ip")
    assert pick_host(2, "203.0.113.9", zone, rec) == ("203.0.113.9", "ip")
    assert pick_host(3, "203.0.113.2", zone, rec) == ("203.0.113.2", "ip")
    assert pick_host(2, "203.0.113.2", zone, set()) == ("203.0.113.2", "ip")
    assert pick_host(2, None, zone, rec) == (None, None)


def test_status_of_a_proxy() -> None:
    now = datetime.now(UTC)
    assert proxy_status("ready", True, now) == "active"
    assert proxy_status("unhealthy", True, now) == "active"  # a short spell keeps posts as they are
    assert proxy_status("repairing", True, now) == "active"
    assert proxy_status("ready", True, None) == "pending"
    assert proxy_status("registering", True, None) == "pending"
    assert proxy_status("burned", True, now) == "burned"
    for out in ("draining", "retiring", "deleted"):
        assert proxy_status(out, True, now) == "expired"
    assert proxy_status("ready", False, now) == "expired"


def _view(**kw: object) -> ProxyView:
    base: dict[str, object] = {
        "key": "mtproto:k",
        "node_id": uuid.uuid4(),
        "node_name": "n",
        "node_state": "ready",
        "is_test": False,
        "slot": 4,
        "user": "mt_x",
        "host": "203.0.113.4",
        "host_kind": "ip",
        "port": 8443,
        "mode": "tls",
        "tls_domain": "www.microsoft.com",
        "enabled": True,
        "sponsor_tag_set": False,
        "connections": 0,
        "total_octets": 0,
        "stats_at": None,
        "live_at": datetime.now(UTC),
        "status": "active",
        "secret_hex": SECRET,
    }
    base.update(kw)
    return ProxyView(**base)  # type: ignore[arg-type]


def test_links_contract_shape() -> None:
    link = to_link(_view())
    assert link is not None
    assert (link.kind, link.slot, link.label, link.expires_at, link.status) == (
        "mtproto",
        4,
        "پروکسی ۴",
        None,
        "active",
    )
    assert link.url == proxy_url("203.0.113.4", 8443, SECRET, "tls", "www.microsoft.com")
    assert to_link(_view(status="burned")).status == "burned"  # type: ignore[union-attr]
    assert to_link(_view(status="expired")).status == "expired"  # type: ignore[union-attr]
    # Never confirmed running, or no host: not published.
    assert to_link(_view(live_at=None, status="pending")) is None
    assert to_link(_view(live_at=None, status="expired")) is None
    assert to_link(_view(host=None, host_kind=None)) is None


def test_a_view_never_prints_its_secret() -> None:
    view = _view()
    assert SECRET not in repr(view) and SECRET not in str(view)
