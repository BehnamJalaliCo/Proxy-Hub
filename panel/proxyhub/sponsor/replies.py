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
are matched by meaning (channel + link; success words without refusal or removal words), and the
first real run records them in actions_log (docs/11 F11): the runbook checks them against these
patterns before the module is relied on. A /myproxies button is pressed only when its label names
the proxy's host (or ``host:port``) with nothing beside it but hex. Anything not recognised is
"unknown": the flow stops there (proxyhub/sponsor/conversation.py). Every text that leaves the
flow goes through :func:`mask`.

Reading the whole /myproxies list and removing a dead proxy from it (proxyhub/sponsor/botlist.py)
use the second half of this module: :func:`list_address` (which proxy a list button names),
:func:`is_next_page` (a strict pagination button), :func:`shown_channels` (the sponsor channel a
proxy's screen names), :func:`removal_done`, and :func:`shape` (a screen as placeholders, safe to
store and report). The removal and confirmation buttons are recognised ONLY in the strict shapes of
:data:`proxyhub.mtproxy.userclient.REMOVE_BUTTON` and ``CONFIRM_BUTTON``, which the operator
client's allowlist enforces too.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from typing import Final

from proxyhub.mtproxy.userclient import (
    CONFIRM_BUTTON,
    REMOVE_BUTTON,
    BotButton,
    BotMessage,
    text_names,
)

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
# /myproxies of an account with no proxy at all (P19, after the owner deleted every proxy by hand):
# "You didn't create any proxy yet. Please use /newproxy command."
NO_PROXIES: Final = re.compile(
    r"\bdid\s*n[o'’]?t\s+create\s+any\s+prox(?:y|ies)\b|\byou\s+have\s+no\s+prox(?:y|ies)\b",
    re.IGNORECASE,
)
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
# Something taken away: never read as "the promotion is set", whatever success word comes with it.
DESTRUCTIVE: Final = re.compile(r"\b(?:delet|remov|revok|disabl|cancel|clear|reset|stopp?)\w*", re.IGNORECASE)
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


def no_proxies(text: str) -> bool:
    """@MTProxybot says the account has no proxy at all (and is not showing a list)."""
    return bool(NO_PROXIES.search(text)) and not is_proxy_list(text)


_HEX_WORD: Final = re.compile(r"[0-9a-f]{1,64}")


def is_address_label(label: str, host: str, port: int) -> bool:
    """The button names this proxy: one word is its ``host`` or ``host:port`` and every other word
    (at most two) is plain hex. The real /myproxies list labels a button "<host> <hex>" (P13 on the
    panel server). Any other word ("Delete 1.2.3.4:443") or another host never matches."""
    words = normalize_label(label).split(" ")
    ours = {host.lower(), f"{host}:{port}".lower()}
    named = [w for w in words if w in ours]
    rest = [w for w in words if w not in ours]
    return len(named) == 1 and len(rest) <= 2 and all(_HEX_WORD.fullmatch(w) for w in rest)


def address_buttons(messages: Iterable[BotMessage], host: str, port: int) -> list[tuple[int, BotButton]]:
    """Pressable buttons that name this proxy (:func:`is_address_label`); the caller presses one only
    when exactly one matches."""
    found: list[tuple[int, BotButton]] = []
    for message in messages:
        for row in message.buttons:
            for button in row:
                if button.data is not None and is_address_label(button.text, host, port):
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
    """Success words, and neither a refusal nor something taken away ("Done! Proxy deleted")."""
    return bool(PROMOTION_DONE.search(text)) and not REFUSAL.search(text) and not DESTRUCTIVE.search(text)


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


# --- the whole /myproxies list, a proxy's screen, removal (proxyhub/sponsor/botlist.py) -----------------

# A proxy address: an IPv4 address or a DNS name whose last label is letters (so "e.g" or "3.5" in
# prose is not one), optionally with ``:port``.
_IPV4: Final = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
_DNS_NAME: Final = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9\-]{0,61}[a-z0-9]"
)
NEXT_PAGE: Final = re.compile(
    r"(?:(?:next|more)(?:\s+page)?\s*(?:»|›|>>|>|→|➡\ufe0f?|▶\ufe0f?|⏩)?"
    r"|(?:»|›|>>|>|→|➡\ufe0f?|▶\ufe0f?|⏩)\s*(?:next|more)?)",
    re.IGNORECASE,
)
REMOVAL_DONE: Final = re.compile(r"\b(?:deleted|removed)\b", re.IGNORECASE)
_NOT_DONE: Final = re.compile(r"\?|\b(?:sure|really|confirm|undo|want to)\b", re.IGNORECASE)
_CHANNEL_REF: Final = re.compile(
    r"(?:(?<![\w@])@|(?<![\w/])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/)"
    r"([A-Za-z][A-Za-z0-9_]{3,31})(?![\w/])",
    re.IGNORECASE,
)
_NOT_CHANNELS: Final = frozenset(
    {"mtproxybot", "proxy", "socks", "share", "addstickers", "addemoji", "joinchat", "setlanguage"}
)
_TME_LINK: Final = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z][A-Za-z0-9_]{3,31}/?", re.IGNORECASE
)
_ANY_URL: Final = re.compile(r"(?:https?|tg)://\S*", re.IGNORECASE)
_SHAPE_HEX: Final = re.compile(r"[0-9a-fA-F]{4,}")
_TOKEN: Final = re.compile(r"(?=[A-Za-z0-9_\-+/=]*\d)[A-Za-z0-9_\-+/=]{16,}")
_EDGE: Final = ".,;:!?()[]{}«»\"'`"
_TEXT_WORDS: Final = re.compile(r"[\s,;()\[\]{}<>«»\"'`|]+")
MAX_SHAPE: Final = 400
TAG_HINT_MIN: Final = 6


def address_of(word: str) -> tuple[str, int | None] | None:
    """``(host, port)`` when ``word`` (lower case) is ``host`` or ``host:port``, else None."""
    host, sep, port_text = word.rpartition(":") if ":" in word else (word, "", "")
    if not (_IPV4.fullmatch(host) or _DNS_NAME.fullmatch(host)):
        return None
    if not sep:
        return host, None
    if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        return None
    return host, int(port_text)


def _list_words(label: str) -> tuple[tuple[str, int | None], list[str]] | None:
    words = normalize_label(label).split(" ")
    found = [a for w in words if (a := address_of(w)) is not None]
    rest = [w for w in words if address_of(w) is None]
    if len(found) != 1 or len(rest) > 2 or not all(_HEX_WORD.fullmatch(w) for w in rest):
        return None
    return found[0], rest


def list_address(label: str) -> tuple[str, int | None] | None:
    """The proxy a /myproxies button names: one word is ``host`` or ``host:port`` and every other
    word (at most two) is plain hex (the real shape is "<host> <hex>", P13). None for anything
    else, so "Delete 1.2.3.4", a page button or a header never counts as a proxy."""
    parsed = _list_words(label)
    return None if parsed is None else parsed[0]


def list_hex_words(label: str) -> tuple[str, ...]:
    """The plain-hex words beside the address of a /myproxies button (held in memory only)."""
    parsed = _list_words(label)
    return () if parsed is None else tuple(parsed[1])


def label_fingerprint(label: str) -> str:
    """A short one-way fingerprint of a list button's label, to find the SAME button again in a
    later read without keeping its hex."""
    return hashlib.sha256(normalize_label(label).encode()).hexdigest()[:16]


def tag_hint(hex_words: Iterable[str], tags: Iterable[str]) -> str | None:
    """The one tag that a hex word of the label starts or ends (at least :data:`TAG_HINT_MIN`
    characters), or None when no tag or more than one does."""
    words = [w.lower() for w in hex_words if len(w) >= TAG_HINT_MIN]
    found = {t for t in tags if any(t.lower().startswith(w) or t.lower().endswith(w) for w in words)}
    return found.pop() if len(found) == 1 else None


def tag_word_matches(word: str, tag: str) -> bool:
    """A hex word of a label or screen names this tag: the whole tag, or at least
    :data:`TAG_HINT_MIN` characters of its start or end (the same rule as :func:`tag_hint`)."""
    word, tag = word.lower(), tag.lower()
    return len(word) >= TAG_HINT_MIN and (tag.startswith(word) or tag.endswith(word))


def label_names_tag(label: str, tag: str) -> bool:
    """A /myproxies button names this tag: one of its hex words is the tag's start or end. Two
    registrations of the same address (Hetzner gave a deleted node's IPv4 to a new one) differ
    only here (P18)."""
    return any(tag_word_matches(w, tag) for w in list_hex_words(label))


_SCREEN_HEX: Final = re.compile(r"(?<![0-9a-z])[0-9a-f]{6,64}(?![0-9a-z])")


def screen_hex_words(text: str) -> list[str]:
    """The hex words (6 to 64 characters) a proxy's screen shows, e.g. its tag in "Proxy <host:port>
    with the tag <hex>." (P18). An address never yields one: its parts are shorter or not hex."""
    return _SCREEN_HEX.findall(text.lower())


def screen_names_other_tag(text: str, tag: str) -> bool:
    """The screen shows a tag, and it is not this one: the screen of another registration of the
    same address. A screen that shows no tag says nothing either way."""
    words = screen_hex_words(text)
    return bool(words) and not any(tag_word_matches(w, tag) for w in words)


def screen_names_any_tag(text: str, tags: Iterable[str]) -> bool:
    """The screen shows one of these tags (a live proxy's: it must never be removed)."""
    words = screen_hex_words(text)
    return any(tag_word_matches(w, t) for w in words for t in tags)


def is_next_page(label: str) -> bool:
    """A pagination button in one of the strict shapes ("Next", "»", "Next »", "➡️", ...)."""
    return bool(NEXT_PAGE.fullmatch(label.strip()))


def is_remove_button(label: str) -> bool:
    return bool(REMOVE_BUTTON.fullmatch(label.strip()))


def is_confirm_button(label: str) -> bool:
    return bool(CONFIRM_BUTTON.fullmatch(label.strip()))


def address_words(host: str, ports: Iterable[int]) -> frozenset[str]:
    """What a screen about this proxy may call it: ``host`` and ``host:port`` for its known ports."""
    host = host.lower()
    return frozenset({host, *(f"{host}:{p}" for p in ports)})


def names_proxy(text: str, words: frozenset[str]) -> bool:
    """One word of the text is the proxy's ``host`` or ``host:port`` (the allowlist's own test)."""
    return text_names(text, words)


def text_addresses(text: str) -> set[str]:
    """Every address-shaped word of ``text`` (lower case, ``host`` or ``host:port``)."""
    found: set[str] = set()
    for raw in _TEXT_WORDS.split(text.lower()):
        word = raw.strip(".!?:")
        if word and address_of(word) is not None:
            found.add(word)
    return found


def names_other_proxy(text: str, words: frozenset[str]) -> bool:
    """The text names an address that is not one of ``words`` (a screen of another proxy)."""
    return bool(text_addresses(text) - words)


def shown_channels(text: str) -> list[str]:
    """The public channels a screen names (``@name`` or ``t.me/name``), lower case, in order."""
    found: list[str] = []
    for match in _CHANNEL_REF.finditer(text):
        name = match.group(1).lower()
        if name not in _NOT_CHANNELS and not name.endswith("_") and name not in found:
            found.append(name)
    return found


def removal_done(text: str) -> bool:
    """The bot says the proxy is gone: "deleted"/"removed", and no question, refusal or doubt."""
    return bool(REMOVAL_DONE.search(text)) and not REFUSAL.search(text) and not _NOT_DONE.search(text)


def _link_shape(match: re.Match[str]) -> str:
    """A link to a public channel stays (lower case); any other link (a proxy link with its
    parameters, ...) becomes ``<link>``, up to the next space."""
    url = match.group(0)
    core = url.rstrip(".,;:!?)]}»\"'")
    return core.lower() + url[len(core) :] if _TME_LINK.fullmatch(core) else "<link>"


def _shape_word(word: str) -> str:
    core = word.strip(_EDGE)
    if not core:
        return word
    start = word.find(core)
    lead, trail = word[:start], word[start + len(core) :]
    if _TME_LINK.fullmatch(core):
        shaped = core.lower()
    elif _ANY_URL.match(core):
        shaped = "<link>"
    elif (address := address_of(core.lower())) is not None:
        shaped = "<host>" if address[1] is None else "<host:port>"
    elif (
        _SHAPE_HEX.fullmatch(core)
        and not core.isdigit()  # a plain number (a count) stays
        and (len(core) >= 8 or any(c.isdigit() for c in core))
    ):
        shaped = "<hex>"
    elif _TOKEN.fullmatch(core):
        shaped = "<token>"
    else:
        shaped = core
    return f"{lead}{shaped}{trail}"


def shape(text: str, *, limit: int = MAX_SHAPE) -> str:
    """What a screen or button of the bot LOOKS like: every secret, proxy address, hex value, link
    and long token replaced by a placeholder (``<host>``, ``<host:port>``, ``<hex>``, ``<link>``,
    ``<token>``). Words, emoji and public channel names (``@name``, ``t.me/name``) stay. Safe to
    store, show and report."""
    masked = _ANY_URL.sub(_link_shape, mask(text))
    lines = [re.sub(r"\S+", lambda m: _shape_word(m.group(0)), line) for line in masked.splitlines()]
    out = "\n".join(line.rstrip() for line in lines).strip()
    return out if len(out) <= limit else out[: limit - 1] + "…"


def scrub(text: str) -> str:
    """For actions_log: :func:`mask`, and every long token (a secret in another encoding) removed."""
    return _TOKEN.sub("[token]", mask(text))
