"""The two conversations with @MTProxybot as a pure state machine (docs/11, روند @MTProxybot).

Registration (3 messages)::

    /newproxy  ->  "host:port"  ->  secret  ->  tag (32 hex)

Promotion (4 messages: 2 texts, 2 button presses)::

    /myproxies  ->  press this proxy's button  ->  press "Set promotion"  ->  https://t.me/<channel>
                ->  confirmation

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
    """Make ``channel`` (a public username) the sponsor of the registered ``host:port``."""

    host: str
    port: int
    channel: str

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


Job = RegisterJob | PromoteJob

MESSAGES_NEEDED: Final[dict[type[RegisterJob] | type[PromoteJob], int]] = {RegisterJob: 3, PromoteJob: 4}


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


def _promote(job: PromoteJob, step: Step, seen: Observation, text: str) -> Action | Result:
    if step is Step.SENT_MYPROXIES:
        if not replies.is_proxy_list(text):
            return _stuck(step, "unexpected_reply", text)
        picked = _single(
            replies.address_buttons(seen.messages, job.host, job.port),
            step,
            "proxy_not_in_list",
            "proxy_listed_twice",
            text,
        )
        if isinstance(picked, Stuck):
            return picked
        message_id, button = picked
        assert button.data is not None
        return Press(message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_PROXY)
    if step is Step.PRESSED_PROXY:
        if replies.mentions_other_address(text, job.host, job.port):
            return _stuck(step, "menu_of_another_proxy", text)
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
