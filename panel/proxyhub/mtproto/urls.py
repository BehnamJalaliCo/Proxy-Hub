"""Pure helpers: proxy keys, Telemt user names, and MTProto proxy links in Telegram's formats.

Link format (core.telegram.org/api/links, "MTProxy links"): ``t.me/proxy?server=<server>&port=<port>
&secret=<secret>``; ``server`` is an IP address or a host name. The secret (core.telegram.org/mtproto/
mtproto-transports, "Transport obfuscation") is 16 bytes in hex; a 17-byte form with a leading
``dd`` selects the padded intermediate transport ("secure"). Fake-TLS secrets are ``ee`` + the
16-byte secret + the hex of the TLS domain (Telemt 3.5.16 ``src/api/users/links.rs`` and FAQ "What do
``dd`` and ``ee`` mean"). We publish the ``https://t.me/proxy`` form (it opens Telegram from any
browser or chat), never ``tg://``.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlencode

from proxyhub.agent_api.validation import HEX32
from proxyhub.mtproto.settings import Mode

KIND = "mtproto"
USER_PREFIX = "mt_"
_KEY = re.compile(
    r"mtproto:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([a-z][a-z0-9_]{0,31})"
)
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def user_name_for(node_id: uuid.UUID) -> str:
    """The node's one long-lived Telemt user: unique across nodes, so it also names the proxy in
    ``mtproxy_registrations.secret_name`` (19 characters, a valid TOML key and metrics label)."""
    return f"{USER_PREFIX}{node_id.hex[:16]}"


def proxy_key(node_id: uuid.UUID, user: str) -> str:
    return f"{KIND}:{node_id}:{user}"


def parse_key(key: str) -> tuple[uuid.UUID, str] | None:
    m = _KEY.fullmatch(key)
    if m is None:
        return None
    return uuid.UUID(m.group(1)), m.group(2)


def link_secret(secret_hex: str, mode: Mode, tls_domain: str) -> str:
    if not HEX32.fullmatch(secret_hex):
        raise ValueError("secret must be 32 lower-case hex characters")
    if mode == "tls":
        return "ee" + secret_hex + tls_domain.encode("ascii").hex()
    if mode == "secure":
        return "dd" + secret_hex
    return secret_hex


def proxy_url(host: str, port: int, secret_hex: str, mode: Mode, tls_domain: str) -> str:
    query = urlencode(
        [("server", host), ("port", str(port)), ("secret", link_secret(secret_hex, mode, tls_domain))]
    )
    return f"https://t.me/proxy?{query}"


def persian_number(n: int) -> str:
    return str(n).translate(_PERSIAN_DIGITS)


def label_for(slot: int | None, is_test: bool) -> str:
    """Short Persian label for a post: "پروکسی ۳" for slot 3."""
    if slot is not None:
        return f"پروکسی {persian_number(slot)}"
    return "پروکسی آزمایشی" if is_test else "پروکسی"
