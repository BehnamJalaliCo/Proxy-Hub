"""Shared value rules for the agent API (both directions).

Rule 7 (no user IPs): every string that reaches us from a node is checked by
:func:`looks_like_ip`, in addition to each field's own narrow pattern, so an address can not be
smuggled in through a version string, an error code or a user name.

Untrusted JSON is walked without recursion and its nesting is bounded (:data:`MAX_JSON_DEPTH`):
``json.loads`` accepts thousands of levels, far past Python's recursion limit, and a
``RecursionError`` escaping a request handler would be a 500 anyone holding nothing but the
proxy secret (which Caddy adds to every ``/agent/v1`` request) could trigger.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
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

# Real agent bodies nest at most four containers (report > telemt > users > user); far more
# than that is not an agent body at all.
MAX_JSON_DEPTH = 16

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


class JsonTooDeep(ValueError):
    """Decoded JSON nests objects/arrays deeper than :data:`MAX_JSON_DEPTH`."""

    def __init__(self) -> None:
        super().__init__("json_too_deep")


def check_json_depth(value: Any, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Raise :class:`JsonTooDeep` if objects/arrays nest more than ``max_depth`` deep (iterative)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        children: Iterable[Any]
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > max_depth:
            raise JsonTooDeep
        stack.extend((child, depth + 1) for child in children if isinstance(child, dict | list))


def find_ip_like(value: Any, max_depth: int = MAX_JSON_DEPTH) -> str | None:
    """Walk decoded JSON (keys and values); return the path of the first IP-looking string.

    Iterative (an explicit stack, no recursion). Raises :class:`JsonTooDeep` when objects/arrays
    nest more than ``max_depth`` deep, so no input can turn into a ``RecursionError``.
    """
    stack: list[tuple[str, Any, int]] = [("$", value, 1)]
    while stack:
        path, item, depth = stack.pop()
        if isinstance(item, str):
            if looks_like_ip(item):
                return path
            continue
        if isinstance(item, dict):
            if depth > max_depth:
                raise JsonTooDeep
            if any(isinstance(key, str) and looks_like_ip(key) for key in item):
                return f"{path}.<key>"
            children = [(f"{path}.{key}", child) for key, child in item.items()]
        elif isinstance(item, list):
            if depth > max_depth:
                raise JsonTooDeep
            children = [(f"{path}[{index}]", child) for index, child in enumerate(item)]
        else:
            continue
        # Reversed, so the walk (and the path it reports) follows document order.
        stack.extend(
            (child_path, child, depth + 1)
            for child_path, child in reversed(children)
            if isinstance(child, str | dict | list)
        )
    return None


def valid_user_name(name: str) -> bool:
    return bool(TELEMT_USER_NAME.fullmatch(name)) and name not in RESERVED_USER_NAMES


def valid_domain(domain: str) -> bool:
    return bool(DOMAIN.fullmatch(domain)) and not looks_like_ip(domain)
