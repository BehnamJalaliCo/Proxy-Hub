"""Public channel links for "Set promotion" (docs/11: @MTProxybot accepts a PUBLIC channel only).

The owner types a link (``https://t.me/name``, ``t.me/name``, ``@name`` or ``name``); the panel keeps
only the username (4 to 32 letters, digits or ``_``, starting with a letter and not ending with
``_``; 4 because short collectible usernames exist)
and sends ``https://t.me/<name>`` to the bot. Invite links (``t.me/+...``, ``t.me/joinchat/...``)
are private and refused. Pure: no I/O.
"""

from __future__ import annotations

import re
from typing import Final

DEFAULT_CHANNEL: Final = "ipoxxo"  # decision D23

_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_LINK: Final = re.compile(
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/)?@?(?P<name>[^/?#\s]+)/?",
    re.IGNORECASE,
)
_PRIVATE: Final = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)", re.IGNORECASE)
# Telegram usernames that are paths of t.me itself, not channels.
_RESERVED: Final = frozenset(
    {"proxy", "socks", "share", "addstickers", "addemoji", "joinchat", "setlanguage"}
)


class ChannelError(ValueError):
    """``code`` is ``channel_private`` or ``channel_format`` (Persian text in sponsor/messages.py)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_public_channel(raw: str) -> str:
    """The channel's username, lower-case, without ``@``; raises :class:`ChannelError`."""
    text = raw.strip()
    if _PRIVATE.search(text) or text.startswith("+"):
        raise ChannelError("channel_private")
    match = _LINK.fullmatch(text)
    if match is None:
        raise ChannelError("channel_format")
    name = match.group("name")
    if not _NAME.fullmatch(name) or name.endswith("_") or name.lower() in _RESERVED:
        raise ChannelError("channel_format")
    return name.lower()


def channel_link(username: str) -> str:
    """What is sent to @MTProxybot for "Set promotion"."""
    return f"https://t.me/{username}"
