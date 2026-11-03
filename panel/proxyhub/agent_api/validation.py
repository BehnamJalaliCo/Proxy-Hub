"""Shared value rules for the agent API (both directions).

Rule 7 (no user IPs): every string that reaches us from a node is checked by
:func:`looks_like_ip`, in addition to each field's own narrow pattern, so an address can not be
smuggled in through a version string, an error code or a user name.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

HEX32 = re.compile(r"[0-9a-f]{32}")
# Telemt user names: TOML bare key and Prometheus label safe. "include" is reserved because Telemt
# expands any config line starting with ``include =`` before parsing; the agent's own placeholder
# name is reserved too.
TELEMT_USER_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
RESERVED_USER_NAMES = frozenset({"include", "agent_placeholder"})
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}")
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,59}")
ETAG = re.compile(r'"[0-9a-f]{64}"')
# A lower-case DNS name with at least one dot; never an IP literal (checked separately).
DOMAIN = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
# Ports a node may never be told to serve proxies on: the agent's local Telemt metrics listener.
RESERVED_PORTS = frozenset({9090})

_IPV4_INSIDE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_IPV6_INSIDE = re.compile(r"(?i)(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}")


def looks_like_ip(value: str) -> bool:
    """True if ``value`` is, or contains something shaped like, an IPv4 or IPv6 address."""
    candidate = value.strip().strip("[]")
    try:
        ipaddress.ip_address(candidate.split("%", 1)[0])
        return True
    except ValueError:
        pass
    if _IPV4_INSIDE.search(value):
        return True
    return any(m.group(0).count(":") >= 2 for m in _IPV6_INSIDE.finditer(value))


def find_ip_like(value: Any, path: str = "$") -> str | None:
    """Walk decoded JSON (keys and values); return the path of the first IP-looking string."""
    if isinstance(value, str):
        return path if looks_like_ip(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and looks_like_ip(key):
                return f"{path}.<key>"
            found = find_ip_like(item, f"{path}.{key}")
            if found:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = find_ip_like(item, f"{path}[{index}]")
            if found:
                return found
    return None


def valid_user_name(name: str) -> bool:
    return bool(TELEMT_USER_NAME.fullmatch(name)) and name not in RESERVED_USER_NAMES


def valid_domain(domain: str) -> bool:
    return bool(DOMAIN.fullmatch(domain)) and not looks_like_ip(domain)
