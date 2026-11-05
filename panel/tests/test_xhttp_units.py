"""XHTTP without a database: the XHTTP link's exact shape, how posts take a config's two links as
one (companion links), the settings defaults, the report field, and the MTProto port it reserves."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import ValidationError

from proxyhub.links.contract import ProxyLink
from proxyhub.mtproto.settings import FORBIDDEN_PORTS, MtprotoSettings
from proxyhub.posts import links
from proxyhub.posts.publisher import fit
from proxyhub.reality.links import config_label, vless_url, vless_xhttp_url, xhttp_key
from proxyhub.reality.report_schema import XrayCounters
from proxyhub.reality.settings import XHTTP_PORT, RealitySettings
from proxyhub.telegram.templates import StatusTexts
from proxyhub.telegram.templates import vless_url as checked_vless

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
USER = uuid.UUID("6a1f3a2e-1111-4c3b-9a55-0123456789ab")
PBK = "A" * 43  # a public key's shape (public link data, not a secret)


def test_the_xhttp_link_has_the_published_shape() -> None:
    url = vless_xhttp_url(
        user_id=USER,
        host="slot3.example.com",
        server_name="dl.google.com",
        public_key=PBK,
        short_id="0123456789abcdef",
        path="/fedcba9876543210",
        name="@ipoxxo 🇫🇮 12 XHTTP",
    )
    assert url == checked_vless(url)  # printable ASCII, bounded: what posts accept
    parts = urlsplit(url)
    assert (parts.username, parts.hostname, parts.port) == (str(USER), "slot3.example.com", 2083)
    assert XHTTP_PORT == 2083
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "encryption": "none",
        "security": "reality",
        "sni": "dl.google.com",
        "fp": "chrome",
        "pbk": PBK,
        "sid": "0123456789abcdef",
        "type": "xhttp",
        "path": "/fedcba9876543210",
        "mode": "auto",
    }
    assert "path=%2Ffedcba9876543210" in url  # URL-encoded like the ws_tls path
    assert unquote(parts.fragment) == "@ipoxxo 🇫🇮 12 XHTTP"
    tcp = vless_url(
        user_id=USER, host="slot3.example.com", server_name="dl.google.com", public_key=PBK,
        short_id="0123456789abcdef", name="@ipoxxo 🇫🇮 12",
    )  # fmt: skip
    assert ":443?" in tcp and "flow=xtls-rprx-vision" in tcp and "flow" not in q
    assert config_label(12) == "کانفیگ ۱۲"
    assert config_label(12, "TCP") == "کانفیگ ۱۲ (TCP)" and config_label(12, "XHTTP") == "کانفیگ ۱۲ (XHTTP)"
    assert xhttp_key("reality:12") == "reality:12:xhttp"


def link(
    key: str, slot: int | None, *, hours: float = 10, companion_of: str | None = None, **over: Any
) -> ProxyLink:
    base: dict[str, Any] = {
        "kind": "reality",
        "key": key,
        "slot": slot,
        "label": "کانفیگ",
        "url": f"vless://{USER}@slot{slot}.example.com:443?type=tcp",
        "expires_at": NOW + timedelta(hours=hours),
        "status": "active",
        "companion_of": companion_of,
    }
    base.update(over)
    return ProxyLink(**base)


def pair(n: int, slot: int, hours: float = 10) -> list[ProxyLink]:
    base = link(f"reality:{n}", slot, hours=hours)
    return [base, link(f"reality:{n}:xhttp", slot, hours=hours, companion_of=base.key)]


@pytest.mark.parametrize("bad", ["", "k" * 201, "reality:1:xhttp"])
def test_checked_refuses_a_bad_companion(bad: str) -> None:
    assert links.checked(link("reality:1:xhttp", 1, companion_of=bad)) is None
    assert links.checked(link("reality:1:xhttp", 1, companion_of="reality:1")) is not None


def test_a_post_counts_a_config_once_and_carries_both_links_together() -> None:
    items = [*pair(1, 1), *pair(2, 2), *pair(3, 3), link("reality:4", 4)]
    h = links.Harvest(links={x.key: x for x in items})
    chosen = links.for_new_post(h, NOW, max_mtproto=0, max_reality=2)
    assert [x.key for x in chosen] == ["reality:1", "reality:1:xhttp", "reality:2", "reality:2:xhttp"]
    turned = links.for_new_post(h, NOW, max_mtproto=0, max_reality=2, rotate=3)
    assert [x.key for x in turned] == ["reality:1", "reality:1:xhttp", "reality:4"]
    # A companion whose config is not chosen, or not listed, never comes alone.
    lone = links.Harvest(links={x.key: x for x in pair(5, 5)[1:]})
    assert links.for_new_post(lone, NOW, max_mtproto=0, max_reality=3) == []
    # A config that ends within the hour is skipped with its XHTTP link.
    ending = links.Harvest(links={x.key: x for x in [*pair(6, 6, hours=0.5), *pair(7, 7)]})
    assert [x.key for x in links.for_new_post(ending, NOW, max_mtproto=0, max_reality=3)] == [
        "reality:7",
        "reality:7:xhttp",
    ]
    # A dead companion stays out of a new post; its config still comes.
    dead = links.Harvest(
        links={
            x.key: x
            for x in [pair(8, 8)[0], link("reality:8:xhttp", 8, companion_of="reality:8", status="burned")]
        }
    )
    assert [x.key for x in links.for_new_post(dead, NOW, max_mtproto=0, max_reality=3)] == ["reality:8"]


def test_a_post_too_long_drops_the_last_xhttp_link_first() -> None:
    long_url = "vless://" + "a" * 1900
    chosen = [
        link(f"reality:{n}{suffix}", n, url=long_url, label=f"کانفیگ {n}")
        for n in (1, 2)
        for suffix in ("", ":xhttp")
    ]
    _, kept = fit("", chosen, "@ipoxxo", StatusTexts())
    assert [x.key for x in kept] == ["reality:1", "reality:1:xhttp"]


def test_settings_and_report_field() -> None:
    assert RealitySettings().xhttp_enabled is True
    stored = RealitySettings().model_dump(mode="json")
    del stored["xhttp_enabled"]  # a value saved before XHTTP existed
    assert RealitySettings.model_validate(stored).xhttp_enabled is True
    base: dict[str, Any] = {
        "enabled": True, "up": True, "version": "26.10.31", "uptime_s": 1.0, "target_ok": None,
        "checked_target": None, "checked_server_name": None, "apply_error": None, "users": [],
    }  # fmt: skip
    assert XrayCounters.model_validate(base).xhttp_port is None  # an agent before 0.4.0
    assert XrayCounters.model_validate({**base, "xhttp_port": 2083}).xhttp_port == 2083
    for bad in (0, 65536, "2083", True):
        with pytest.raises(ValidationError):
            XrayCounters.model_validate({**base, "xhttp_port": bad})


def test_mtproto_can_not_take_the_xhttp_port() -> None:
    assert XHTTP_PORT in FORBIDDEN_PORTS
    with pytest.raises(ValidationError):
        MtprotoSettings(port=XHTTP_PORT)
    assert MtprotoSettings(port=8443).port == 8443
