"""Runs one conversation (proxyhub/sponsor/conversation.py) over the chat with @MTProxybot.

For each action (:func:`perform`): take a pacing slot (5 s gap, 30 per hour, Telegram's hold, kill
switch; proxyhub/sponsor/pacing.py), write the message to actions_log (before it goes out, so a
message Telegram received while the call failed is on record too: lock 5), then send the text
(a secret only with ``sensitive=True``, which keeps it out of every log line) or press the button.
Then read the chat every ``poll_s`` until the bot's new or changed messages arrive (one more read
lets a burst of messages settle), write the reply to actions_log with every secret masked, and ask
the state machine what is next.
It stops at the first result (tag, promotion set, or a reply it does not understand) or at the
first failure, and never raises for Telegram's or the pace's reasons: the caller gets a
:class:`FlowResult`. No database transaction is open while Telegram is called.

:func:`walk` does the same for the list walks of proxyhub/sponsor/botlist.py (reading every page of
/myproxies, a proxy's screen, removing a dead proxy), whose state machines keep their own memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from proxyhub.mtproxy.userclient import BotMessage, MtpError, RequestNotAllowed
from proxyhub.sponsor import replies
from proxyhub.sponsor.conversation import (
    Forgotten,
    Job,
    Observation,
    Press,
    PromotionSet,
    Registered,
    RegisterJob,
    Send,
    Step,
    Stuck,
    first_action,
    next_action,
)
from proxyhub.sponsor.pacing import PaceStop

MAX_ACTIONS: Final = 8  # a promotion: 4 messages plus up to 4 list pages
REFUSED: Final = "request_not_allowed"


class Chat(Protocol):
    """What the driver needs of :class:`proxyhub.mtproxy.userclient.BotChat`."""

    async def send(self, text: str, *, sensitive: bool = False) -> int | None: ...
    async def recent(self, limit: int = ...) -> list[BotMessage]: ...
    async def press(self, message_id: int, data: bytes) -> str | None: ...


class Recorder(Protocol):
    """actions_log lines, one short transaction each (proxyhub/sponsor/service.py). ``sending`` and
    ``pressing`` are written before the request goes out."""

    async def sending(self, step: Step, shown: str) -> None: ...
    async def pressing(self, step: Step, label: str) -> None: ...
    async def reply(self, step: Step, text: str, buttons: list[str]) -> None: ...


@dataclass(frozen=True)
class Timing:
    poll_s: float = 2.0
    settle_s: float = 2.0
    reply_timeout_s: float = 30.0


@dataclass(frozen=True)
class FlowResult:
    """``result`` when the conversation reached one; else ``error`` (a code) and, for a flood
    wait, ``retry_after_s``. ``last_bot_text`` is masked."""

    result: Registered | PromotionSet | Forgotten | Stuck | None = None
    error: str | None = None
    rpc: str | None = None
    retry_after_s: int | None = None
    last_step: Step | None = None
    last_bot_text: str | None = None
    messages_sent: int = 0


def changes(before: Sequence[BotMessage], after: Sequence[BotMessage]) -> list[BotMessage]:
    """The bot's messages in ``after`` that are new since ``before`` (a higher id than any seen)
    or that the bot edited (other text, buttons or edit time). Our own messages never count."""
    old = {m.id: m for m in before}
    top = max(old, default=0)
    found: list[BotMessage] = []
    for message in after:
        if message.outgoing:
            continue
        prev = old.get(message.id)
        if prev is None:
            if message.id > top:
                found.append(message)
        elif (message.text, message.buttons, message.edit_date) != (prev.text, prev.buttons, prev.edit_date):
            found.append(message)
    return found


def button_labels(messages: Sequence[BotMessage]) -> list[str]:
    return [button.text for message in messages for row in message.buttons for button in row]


async def _await_reply(
    chat: Chat, before: list[BotMessage], timing: Timing, sleep: Callable[[float], Awaitable[None]]
) -> tuple[list[BotMessage], list[BotMessage]]:
    """(the bot's new or changed messages, the latest view of the chat)."""
    latest = before
    waited = 0.0
    while waited < timing.reply_timeout_s:
        await sleep(timing.poll_s)
        waited += timing.poll_s
        latest = await chat.recent()
        if changes(before, latest):
            await sleep(timing.settle_s)
            latest = await chat.recent()
            return changes(before, latest), latest
    return [], latest


async def perform(
    chat: Chat,
    action: Send | Press,
    *,
    take_slot: Callable[[], Awaitable[object]],
    record: Recorder,
    secrets: Sequence[str] = (),
) -> str | None:
    """ONE message to the bot: the pacing slot first (raises :class:`PaceStop`), then its
    actions_log line, then the request. A press returns the bot's short answer, if any."""
    await take_slot()
    if isinstance(action, Send):
        await record.sending(action.then, action.shown)
        await chat.send(action.text, sensitive=action.sensitive)
        return None
    await record.pressing(action.then, replies.mask(action.label, secrets=secrets))
    return await chat.press(action.message_id, action.data)


async def run(
    chat: Chat,
    job: Job,
    *,
    take_slot: Callable[[], Awaitable[object]],
    record: Recorder,
    timing: Timing,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FlowResult:
    secrets = [job.secret_hex] if isinstance(job, RegisterJob) else []
    step: Step | None = None
    sent = 0
    try:
        before = await chat.recent()
        action: Send | Press = first_action(job)
        for _ in range(MAX_ACTIONS):
            answer = await perform(chat, action, take_slot=take_slot, record=record, secrets=secrets)
            sent += 1
            step = action.then
            found, before = await _await_reply(chat, before, timing, sleep)
            if not found and not answer:
                return FlowResult(error="no_reply", last_step=step, messages_sent=sent)
            seen = Observation(messages=tuple(found), answer=answer)
            nxt = next_action(job, step, seen)
            tags = [nxt.tag] if isinstance(nxt, Registered) else []
            masked = replies.mask(seen.text, secrets=secrets, tags=tags)
            await record.reply(
                step, masked, [replies.mask(b, secrets=secrets, tags=tags) for b in button_labels(found)]
            )
            if isinstance(nxt, Send | Press):
                action = nxt
                continue
            return FlowResult(
                result=nxt,
                last_step=step,
                last_bot_text=masked if isinstance(nxt, Stuck) else None,
                messages_sent=sent,
            )
        return FlowResult(error="too_many_steps", last_step=step, messages_sent=sent)
    except MtpError as exc:
        return FlowResult(
            error=exc.code, rpc=exc.rpc, retry_after_s=exc.seconds, last_step=step, messages_sent=sent
        )
    except PaceStop as exc:
        return FlowResult(error=exc.reason, last_step=step, messages_sent=sent)


# --- the list walks (proxyhub/sponsor/botlist.py) ---------------------------------------------------------


class Machine(Protocol):
    """A walk as a pure state machine with its own memory: the first message, then, for each reply,
    the next action or a result (anything that is not a :class:`Send` or :class:`Press`)."""

    def first(self) -> Send: ...
    def next(self, step: Step, seen: Observation) -> object: ...


@dataclass(frozen=True)
class WalkResult:
    """``result`` when the machine reached one, else ``error``. ``attempted_step``: the step of the
    last message that was TRIED (it may have reached the bot even when the call failed), so a walk
    that stopped after a removal button can never be taken for one that did not press it."""

    result: object | None = None
    error: str | None = None
    rpc: str | None = None
    retry_after_s: int | None = None
    last_step: Step | None = None
    attempted_step: Step | None = None
    messages_sent: int = 0


async def walk(
    chat: Chat,
    machine: Machine,
    *,
    take_slot: Callable[[], Awaitable[object]],
    record: Recorder,
    timing: Timing,
    max_actions: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> WalkResult:
    """:func:`run` for a :class:`Machine`: the same slot, log line, request and wait for each
    action, every reply logged with :func:`proxyhub.sponsor.replies.scrub`. Never raises for
    Telegram's or the pace's reasons."""
    step: Step | None = None
    tried: dict[str, Step] = {}
    current: dict[str, Send | Press] = {}
    sent = 0

    async def slot() -> object:
        taken = await take_slot()
        tried["step"] = current["action"].then  # from here on, the message may reach the bot
        return taken

    try:
        before = await chat.recent()
        current["action"] = machine.first()
        for _ in range(max_actions):
            answer = await perform(chat, current["action"], take_slot=slot, record=record)
            sent += 1
            step = current["action"].then
            found, before = await _await_reply(chat, before, timing, sleep)
            if not found and not answer:
                return WalkResult(error="no_reply", last_step=step, attempted_step=step, messages_sent=sent)
            seen = Observation(messages=tuple(found), answer=answer)
            await record.reply(
                step, replies.scrub(seen.text), [replies.scrub(b) for b in button_labels(found)]
            )
            nxt = machine.next(step, seen)
            if isinstance(nxt, Send | Press):
                current["action"] = nxt
                continue
            return WalkResult(result=nxt, last_step=step, attempted_step=step, messages_sent=sent)
        return WalkResult(error="too_many_steps", last_step=step, attempted_step=step, messages_sent=sent)
    except RequestNotAllowed:
        # The operator client refused a request before it left (its allowlist and the machine
        # disagree): a stop, never a retry. Nothing of that request reached Telegram.
        return WalkResult(error=REFUSED, last_step=step, attempted_step=step, messages_sent=sent)
    except MtpError as exc:
        return WalkResult(
            error=exc.code,
            rpc=exc.rpc,
            retry_after_s=exc.seconds,
            last_step=step,
            attempted_step=tried.get("step"),
            messages_sent=sent,
        )
    except PaceStop as exc:
        return WalkResult(
            error=exc.reason, last_step=step, attempted_step=tried.get("step"), messages_sent=sent
        )
