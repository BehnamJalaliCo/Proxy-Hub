"""Reading @MTProxybot's replies (pure, no I/O).

@MTProxybot has no API and no documentation (docs/11 S4). These patterns come from the replies
people have published (kept word for word as test data in tests/test_sponsor_replies.py):

* after ``/newproxy``: "Registering a new proxy server. Please send me its address in the format
  host:port."
* after the address: "Now please specify its secret in hex format."
* after the secret: "Success!" / "Your proxy has been successfully registered. You can now pass this
  proxy tag to the software you are using:" / the 32-hex tag / "Here is a link to your proxy
  server:" (a link that ECHOES THE SECRET) / "And here is a direct link for those who have the
  Telegram app installed:" (the same link as tg://),
* after ``/myproxies``: "Here is the list of all proxies you created:" with one inline button per
  proxy, and the proxy's menu has a "Set promotion" button (telemt docs/FAQ.en.md).

The channel prompt and the confirmation after the channel link are not published anywhere: they
are matched by meaning (channel + link; success words without refusal words), and the first real
run records them in actions_log (docs/11 F11). Anything not recognised is "unknown": the flow stops
there (proxyhub/sponsor/conversation.py). Every text that leaves the flow goes through :func:`mask`.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from typing import Final

from proxyhub.mtproxy.userclient import BotButton, BotMessage

MAX_STORED_TEXT: Final = 1500

_HEX32: Final = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32}(?![0-9A-Za-z])")
_HEX_RUN: Final = re.compile(r"[0-9a-fA-F]{32,}")
_URL: Final = re.compile(r"(?:https?|tg)://\S+", re.IGNORECASE)
_SECRET_PARAM: Final = re.compile(r"(secret=)[^&\s]+", re.IGNORECASE)

ASK_ADDRESS: Final = re.compile(
    r"send me its address in the format\s+host:port|\baddress\b[^\n]{0,80}\bhost:port\b", re.IGNORECASE
)
ASK_SECRET: Final = re.compile(
    r"specify its secret in hex format|\bsecret\b[^\n]{0,60}\bhex\b", re.IGNORECASE
)
REGISTERED: Final = re.compile(r"successfully registered|pass this proxy tag", re.IGNORECASE)
PROXY_LIST: Final = re.compile(r"list of all proxies you created", re.IGNORECASE)
ASK_CHANNEL: Final = re.compile(
    r"\bchannel\b[^\n]{0,120}\b(?:link|username|send|enter|specify|provide)\b"
    r"|\b(?:link|username|send|enter|specify|provide)\b[^\n]{0,120}\bchannel\b",
    re.IGNORECASE,
)
PROMOTION_DONE: Final = re.compile(
    r"\bsuccess(?:fully)?\b|\bdone\b|\bsaved\b|\bhas been (?:set|updated|changed|saved)\b"
    r"|\bpromotion (?:is )?(?:set|updated|enabled|activated)\b|\bwill be (?:shown|displayed)\b",
    re.IGNORECASE,
)
# Words of a refusal. Not used for the channel prompt, which may well say "private channels are
# not supported" while asking for the link.
REFUSAL: Final = re.compile(
    r"\b(?:error|invalid|incorrect|wrong|sorry|cannot|can't|can not|unable|not found|failed|fail"
    r"|denied|forbidden|private|admin(?:istrator)?|already|not allowed|not a public|isn't|is not)\b",
    re.IGNORECASE,
)
HARD_ERROR: Final = re.compile(r"\b(?:error|invalid|incorrect|wrong|sorry|failed|unable)\b", re.IGNORECASE)
# "Set promotion" is the published label; the others are the same action worded for a proxy that
# already has one (assumed, never seen). "Remove/Delete ..." can never match.
PROMOTION_LABELS: Final = frozenset(
    {"set promotion", "change promotion", "edit promotion", "update promotion"}
)
_LABEL_NOISE: Final = re.compile(r"[^\w\s:.\-]", re.UNICODE)
ZERO_TAG: Final = "0" * 32


def normalize_label(text: str) -> str:
    """A button label without emoji or punctuation, lower-case, single spaces."""
    return " ".join(_LABEL_NOISE.sub(" ", text).casefold().split())


def joined(messages: Iterable[BotMessage]) -> str:
    return "\n".join(m.text for m in messages if m.text)


def asks_address(text: str) -> bool:
    return bool(ASK_ADDRESS.search(text)) and not HARD_ERROR.search(text)


def asks_secret(text: str) -> bool:
    return bool(ASK_SECRET.search(text)) and not HARD_ERROR.search(text)


def registered_tag(text: str, secret_hex: str) -> str | None:
    """The proxy tag from the success reply: exactly one 32-hex token outside the links, and not
    the secret we sent (the bot's links echo it). None when the reply is anything else."""
    if not REGISTERED.search(text):
        return None
    outside_links = _URL.sub(" ", text)
    tokens = {t.lower() for t in _HEX32.findall(outside_links)} - {secret_hex.lower()}
    if len(tokens) != 1:
        return None
    tag = tokens.pop()
    return None if tag == ZERO_TAG else tag


def is_proxy_list(text: str) -> bool:
    return bool(PROXY_LIST.search(text))


def address_buttons(messages: Iterable[BotMessage], host: str, port: int) -> list[tuple[int, BotButton]]:
    """Pressable buttons whose label is exactly this proxy's ``host:port`` (spaces and emoji aside)."""
    want = f"{host}:{port}".lower()
    found: list[tuple[int, BotButton]] = []
    for message in messages:
        for row in message.buttons:
            for button in row:
                if button.data is not None and want in normalize_label(button.text).split():
                    found.append((message.id, button))
    return found


def promotion_buttons(messages: Iterable[BotMessage]) -> list[tuple[int, BotButton]]:
    return [
        (message.id, button)
        for message in messages
        for row in message.buttons
        for button in row
        if button.data is not None and normalize_label(button.text) in PROMOTION_LABELS
    ]


_ADDRESS: Final = re.compile(r"\b[a-z0-9][a-z0-9.\-]*\.[a-z0-9\-]+:\d{1,5}\b", re.IGNORECASE)


def mentions_other_address(text: str, host: str, port: int) -> bool:
    """The text names some ``host:port`` and not ours (a menu of another proxy)."""
    named = {a.lower() for a in _ADDRESS.findall(text)}
    return bool(named) and f"{host}:{port}".lower() not in named


def asks_channel(text: str) -> bool:
    return bool(ASK_CHANNEL.search(text)) and not HARD_ERROR.search(text)


def promotion_done(text: str) -> bool:
    return bool(PROMOTION_DONE.search(text)) and not REFUSAL.search(text)


def refusal_hint(text: str) -> str | None:
    """A code for the owner's alert when the reply looks like a known kind of refusal."""
    lowered = text.lower()
    if re.search(r"\badmin(?:istrator)?\b", lowered):
        return "admin_required"  # docs/11 F12
    if re.search(r"\bprivate\b|\bnot a public\b", lowered):
        return "channel_private"
    if re.search(r"\balready\b", lowered):
        return "already_registered"
    if re.search(r"\b(?:invalid|incorrect|wrong)\b", lowered):
        return "invalid_input"
    if re.search(r"\b(?:too many|later|limit)\b", lowered):
        return "bot_limit"
    return None


# --- masking -------------------------------------------------------------------------------------------


def short_tag(tag: str | None) -> str | None:
    """A tag as shown in logs and the dashboard: its first and last four characters."""
    if tag is None:
        return None
    return f"{tag[:4]}…{tag[-4:]}" if len(tag) > 8 else "…"


def _encodings(secret_hex: str) -> set[str]:
    try:
        raw = bytes.fromhex(secret_hex)
    except ValueError:
        return {secret_hex}
    forms = {secret_hex.lower(), secret_hex.upper()}
    for encoded in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
        forms |= {encoded, encoded.rstrip("=")}
    return forms


def mask(text: str, *, secrets: Iterable[str] = (), tags: Iterable[str] = ()) -> str:
    """``text`` safe to store and show: every proxy secret (hex and base64 forms) and every
    ``secret=`` value removed, every other run of 32+ hex characters removed, a known tag
    shortened. Truncated to :data:`MAX_STORED_TEXT` characters."""
    for secret in secrets:
        for form in sorted(_encodings(secret), key=len, reverse=True):
            if len(form) >= 8:
                text = re.sub(re.escape(form), "[secret]", text, flags=re.IGNORECASE)
    text = _SECRET_PARAM.sub(r"\1[secret]", text)
    known = {t.lower() for t in tags}

    def _hex(match: re.Match[str]) -> str:
        value = match.group(0).lower()
        return (short_tag(value) or "") if value in known else "[hex]"

    text = _HEX_RUN.sub(_hex, text)
    if len(text) > MAX_STORED_TEXT:
        text = text[: MAX_STORED_TEXT - 1] + "…"
    return text
