"""The two conversations with @MTProxybot as a pure state machine (docs/11, روند @MTProxybot).

Registration (3 messages)::

    /newproxy  ->  "host:port"  ->  secret  ->  tag (32 hex)

Promotion (4 messages: 2 texts, 2 button presses, plus one press per further page of the list)::

    /myproxies  ->  (next page)*  ->  press this proxy's button  ->  press "Set promotion"
                ->  https://t.me/<channel>  ->  confirmation

The list names a button "<host> <hex>", the hex being a piece of the registration's tag (P13, P18).
Hetzner may give a deleted node's IPv4 to a new one, so the list can hold two buttons with the same
address: the old registration's and ours. With the tag known, only the button that names OUR tag is
pressed, on whichever page it is; the proxy's screen ("Proxy <host:port> with the tag <hex>") must
not name another tag either. A list that shows the address but not our tag stops the flow.

:func:`first_action` gives the opening message; :func:`next_action` reads what the bot said after
our last action (its new or changed messages, and the short answer to a button press) and gives the
next action or a terminal result. A reply that is not exactly what the step expects gives
:class:`Stuck` with a reason code: the driver stops, nothing is guessed. No I/O here; the tests feed
real reply texts as data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from proxyhub.mtproxy.userclient import BotButton, BotMessage
from proxyhub.sponsor import replies
from proxyhub.sponsor.channels import channel_link


class Step(StrEnum):
    SENT_NEWPROXY = "sent_newproxy"
    SENT_ADDRESS = "sent_address"
    SENT_SECRET = "sent_secret"  # noqa: S105 - a step name
    SENT_MYPROXIES = "sent_myproxies"
    PRESSED_PROXY = "pressed_proxy"
    PRESSED_PROMOTION = "pressed_promotion"
    SENT_CHANNEL = "sent_channel"
    # Reading the whole /myproxies list and removing a dead proxy (proxyhub/sponsor/botlist.py).
    PRESSED_NEXT_PAGE = "pressed_next_page"
    PRESSED_REMOVE = "pressed_remove"
    PRESSED_CONFIRM = "pressed_confirm"


@dataclass(frozen=True)
class RegisterJob:
    """Register ``host:port`` with this secret (32 lower-case hex, held in memory only)."""

    host: str
    port: int
    secret_hex: str

    def __repr__(self) -> str:
        return f"RegisterJob(host={self.host!r}, port={self.port}, secret_hex=***)"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class PromoteJob:
    """Make ``channel`` (a public username) the sponsor of the registered ``host:port``. ``tag``:
    the registration's tag, to tell its list button from another registration of the same address."""

    host: str
    port: int
    channel: str
    tag: str | None = None

    def __repr__(self) -> str:
        tag = "***" if self.tag else None
        return f"PromoteJob(host={self.host!r}, port={self.port}, channel={self.channel!r}, tag={tag})"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


Job = RegisterJob | PromoteJob

# A promotion may need one press more: the list's second page (P18: 10 proxies, 2 pages).
MESSAGES_NEEDED: Final[dict[type[RegisterJob] | type[PromoteJob], int]] = {RegisterJob: 3, PromoteJob: 5}


@dataclass(frozen=True)
class Send:
    text: str
    shown: str  # what actions_log records (the secret is never shown)
    then: Step
    sensitive: bool = False

    def __repr__(self) -> str:
        return f"Send(shown={self.shown!r}, then={self.then})"


@dataclass(frozen=True)
class Press:
    message_id: int
    data: bytes
    label: str
    then: Step


@dataclass(frozen=True)
class Registered:
    tag: str


@dataclass(frozen=True)
class PromotionSet:
    channel: str


@dataclass(frozen=True)
class Stuck:
    step: Step
    reason: str
    hint: str | None = None


Action = Send | Press
Result = Registered | PromotionSet | Stuck


@dataclass(frozen=True)
class Observation:
    """What changed in the chat after our action: the bot's new or edited messages (oldest first)
    and, after a button press, the bot's short answer."""

    messages: tuple[BotMessage, ...]
    answer: str | None = None

    @property
    def text(self) -> str:
        parts = [replies.joined(self.messages)]
        if self.answer:
            parts.append(self.answer)
        return "\n".join(p for p in parts if p)


def first_action(job: Job) -> Send:
    if isinstance(job, RegisterJob):
        return Send(text="/newproxy", shown="/newproxy", then=Step.SENT_NEWPROXY)
    return Send(text="/myproxies", shown="/myproxies", then=Step.SENT_MYPROXIES)


def _stuck(step: Step, reason: str, text: str) -> Stuck:
    return Stuck(step=step, reason=reason, hint=replies.refusal_hint(text))


def next_action(job: Job, step: Step, seen: Observation) -> Action | Result:
    text = seen.text
    if isinstance(job, RegisterJob):
        return _register(job, step, seen, text)
    return _promote(job, step, seen, text)


def _register(job: RegisterJob, step: Step, seen: Observation, text: str) -> Action | Result:
    if step is Step.SENT_NEWPROXY:
        if replies.asks_address(text):
            return Send(text=job.address, shown=job.address, then=Step.SENT_ADDRESS)
        return _stuck(step, "unexpected_reply", text)
    if step is Step.SENT_ADDRESS:
        if replies.asks_secret(text):
            return Send(text=job.secret_hex, shown="[secret]", then=Step.SENT_SECRET, sensitive=True)
        return _stuck(step, "address_refused", text)
    if step is Step.SENT_SECRET:
        tag = replies.registered_tag(text, job.secret_hex)
        if tag is not None:
            return Registered(tag=tag)
        return _stuck(step, "no_tag_in_reply", text)
    return Stuck(step=step, reason="wrong_step")


def _single(
    found: Sequence[tuple[int, BotButton]], step: Step, none: str, many: str, text: str
) -> tuple[int, BotButton] | Stuck:
    if not found:
        return _stuck(step, none, text)
    if len({(message_id, button.data) for message_id, button in found}) > 1:
        return _stuck(step, many, text)
    return found[0]


def _list_buttons(job: PromoteJob, seen: Observation) -> list[tuple[int, BotButton]]:
    """The list buttons of this proxy: same address and, with the tag known, naming our tag."""
    found = replies.address_buttons(seen.messages, job.host, job.port)
    if job.tag is None or not any(replies.list_hex_words(b.text) for _, b in found):
        return found  # labels without hex: the address alone decides, as before P18
    tag = job.tag
    return [(message_id, b) for message_id, b in found if replies.label_names_tag(b.text, tag)]


def _next_page_buttons(seen: Observation) -> list[tuple[int, BotButton]]:
    return [
        (message.id, button)
        for message in seen.messages
        for row in message.buttons
        for button in row
        if button.data is not None and replies.is_next_page(button.text)
    ]


def _in_list(job: PromoteJob, step: Step, seen: Observation, text: str) -> Action | Result:
    if step is Step.SENT_MYPROXIES and not replies.is_proxy_list(text):
        return _stuck(step, "unexpected_reply", text)
    if step is Step.PRESSED_NEXT_PAGE and not seen.messages:  # only a toast: the page did not change
        return _stuck(step, "proxy_not_in_list", text)
    mine = _list_buttons(job, seen)
    if mine:
        picked = _single(mine, step, "proxy_not_in_list", "proxy_listed_twice", text)
        if isinstance(picked, Stuck):
            return picked
        message_id, button = picked
        assert button.data is not None
        return Press(message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_PROXY)
    pages = _next_page_buttons(seen)
    if len({(message_id, b.data) for message_id, b in pages}) == 1:
        message_id, button = pages[0]
        assert button.data is not None
        return Press(message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_NEXT_PAGE)
    if job.tag is not None and replies.address_buttons(seen.messages, job.host, job.port):
        return _stuck(step, "tag_not_in_list", text)
    return _stuck(step, "proxy_not_in_list", text)


def _promote(job: PromoteJob, step: Step, seen: Observation, text: str) -> Action | Result:
    if step in (Step.SENT_MYPROXIES, Step.PRESSED_NEXT_PAGE):
        return _in_list(job, step, seen, text)
    if step is Step.PRESSED_PROXY:
        if replies.mentions_other_address(text, job.host, job.port):
            return _stuck(step, "menu_of_another_proxy", text)
        if job.tag is not None and replies.screen_names_other_tag(text, job.tag):
            return _stuck(step, "menu_of_another_tag", text)
        picked = _single(
            replies.promotion_buttons(seen.messages),
            step,
            "no_set_promotion_button",
            "several_promotion_buttons",
            text,
        )
        if isinstance(picked, Stuck):
            return picked
        message_id, button = picked
        assert button.data is not None
        return Press(message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_PROMOTION)
    if step is Step.PRESSED_PROMOTION:
        if replies.asks_channel(text):
            link = channel_link(job.channel)
            return Send(text=link, shown=link, then=Step.SENT_CHANNEL)
        return _stuck(step, "no_channel_prompt", text)
    if step is Step.SENT_CHANNEL:
        if replies.promotion_done(text):
            return PromotionSet(channel=job.channel)
        return _stuck(step, "channel_refused", text)
    return Stuck(step=step, reason="wrong_step")
