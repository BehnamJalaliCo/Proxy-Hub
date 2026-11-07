"""@MTProxybot's own list of our proxies: reading all of it, and removing the proxies whose node was
deleted (pure state machines and mapping; no I/O). The I/O is proxyhub/sponsor/driver.py's
:func:`~proxyhub.sponsor.driver.walk`, the bookkeeping proxyhub/sponsor/inventory.py.

@MTProxybot has no documentation (docs/11 S4). What is known from real traffic: ``/myproxies``
answers "Here is the list of all proxies you created:" with one inline button per proxy, labelled
"<host> <hex>" (P13), and a proxy's button opens its screen, which has "Set promotion" (pagination
and the screen: P18, below). Nothing is known of the confirmation of a removal, so every step below
presses only a button in a strict,
documented shape, checks the screen it lands on, and otherwise STOPS with a reason and the screens
it saw as shapes (:func:`proxyhub.sponsor.replies.shape`: addresses, hex values, links and tokens
replaced; no secret):

* :class:`ListWalk` (the inventory): ``/myproxies``; the reply must carry the list header. Every
  button whose label is a proxy address with at most two hex words beside it
  (:func:`~proxyhub.sponsor.replies.list_address`) is an entry. A "next page" button in a strict
  shape (:data:`~proxyhub.sponsor.replies.NEXT_PAGE`: "Next", "More", "»", "›", ">", "→", "➡️",
  "▶️", "⏩", optionally with "page" and an arrow) is pressed while there is exactly one, up to
  :data:`MAX_PAGES` pages; a page seen before ends the walk. Nothing else is pressed.
* :class:`MenuWalk` with ``remove=False`` (the promotion check of a live proxy, and the "observe"
  run): ``/myproxies``, pages until the button with the target's label (same address, same label
  fingerprint) is found exactly once, press it, and read the proxy's screen: the public channels it
  names (the Proxy Sponsor), whether it names the proxy, and how many removal buttons it has.
  Nothing more is pressed.
* :class:`MenuWalk` with ``remove=True`` (the cleanup, only for a proxy whose node was deleted and
  only once the owner turned real removal on): the same walk to the proxy's screen, which must name
  the proxy and no other address and have exactly ONE button of the shape
  ``^(🗑\\s*)?(Delete|Remove)( proxy)?$`` (:data:`~proxyhub.mtproxy.userclient.REMOVE_BUTTON`). It is
  pressed. The screen after it must name the proxy and no other address and have exactly ONE
  button of the shape ``^(✅\\s*)?(Yes|Yes, delete( it)?|Yes, remove( it)?|Yes, I'm 100% sure,
  delete my proxy|Confirm)[.!]?$`` (:data:`~proxyhub.mtproxy.userclient.CONFIRM_BUTTON`); it is
  pressed, and the answer must say "deleted", "removed" or the bot's bare "Success!" without a
  question, refusal or doubt (:func:`~proxyhub.sponsor.replies.removal_confirmed`). A bot that
  removes at once (no confirmation button, and the screen already says "deleted"/"removed") is
  accepted too.

The operator client's allowlist enforces the same: a removal or confirmation button passes only
with the connection's removal permit and under a message that names the permitted address
(proxyhub/mtproxy/userclient.py).

:func:`classify` maps each entry to our own rows: ``live`` (a registration or a proxy waiting for
one uses this address), ``gone`` (every registration of ours at this address is retired and its
node was deleted), ``retired`` (retired, but its node still exists, e.g. a new address), or
``unknown`` (nothing of ours at this address). Only ``gone`` entries that can be told apart are
``removable``; ``live`` and ``unknown`` ones never are. Since P18 two more kinds are ``gone``: an
older registration of a live address (Hetzner gave a deleted node's IPv4 again, :data:`OLD_TAG`)
and an address of ours whose rows were deleted by hand while nothing holds it now
(:data:`NO_RECORD`); see :func:`classify`. A removal also stops on a proxy screen that shows the tag
of one of our live registrations.

P18 saw the real list and screens: 10 buttons on 2 pages, two to a row, "»" under page 1 and "«"
under page 2; a proxy's screen "Proxy <host:port> with the tag <hex>.\nPromoted channel: n/a.\nHow
can I help?" with [Set promotion, Stats] and [Delete proxy, « Back to list]. The confirmation after
"Delete proxy" (the owner's screenshot, 2026-11-07): "Are you sure you want to de-register the proxy
...<last 8 hex of its tag>with address <host:port>?\nIt will continue working for users until you
disable it on your servers, but it will be hidden from your list and channel promotion will be
disabled." with [Yes, I'm 100% sure, delete my proxy.] and [« Back to proxy, « Back to list]. The
answer after the confirmation has not been seen yet."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from proxyhub.mtproxy.userclient import BotButton, BotMessage
from proxyhub.sponsor import replies
from proxyhub.sponsor.conversation import Observation, Press, Send, Step
from proxyhub.sponsor.provider import node_of
from proxyhub.sponsor.store import RETIRED, Reg

MAX_PAGES: Final = 8
MAX_WALK_ACTIONS: Final = MAX_PAGES + 4
MAX_LABEL_SHAPE: Final = 120
MAX_BUTTON_SHAPE: Final = 80

LIVE: Final = "live"
GONE: Final = "gone"
RETIRED_ALIVE: Final = "retired"
UNKNOWN: Final = "unknown"
GONE_NODE_STATES: Final = frozenset({"deleted"})
GONE_REASONS: Final = frozenset({"node_gone", "proxy_gone"})


# --- what the bot showed, as shapes ----------------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    """One screen of the bot as placeholders (:func:`proxyhub.sponsor.replies.shape`)."""

    stage: str
    text: str
    buttons: tuple[tuple[str, ...], ...]
    answer: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "text": self.text,
            "buttons": [list(row) for row in self.buttons],
            "answer": self.answer,
        }


def screen_of(stage: str, seen: Observation) -> Screen:
    rows = tuple(
        tuple(replies.shape(b.text, limit=MAX_BUTTON_SHAPE) for b in row)
        for message in seen.messages
        for row in message.buttons
    )
    return Screen(
        stage=stage,
        text=replies.shape(replies.joined(seen.messages)),
        buttons=rows,
        answer=replies.shape(seen.answer) if seen.answer else None,
    )


# --- the list ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListEntry:
    """One proxy button of /myproxies: the address it names, the label's shape and fingerprint. The
    label's hex words are held in memory only (to match one of our tags) and never stored."""

    host: str
    port: int | None
    label_shape: str
    label_fp: str
    page: int
    position: int
    pressable: bool
    hex_words: tuple[str, ...] = field(default=(), repr=False)

    @property
    def address(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Page:
    entries: tuple[tuple[int, BotButton, ListEntry], ...]
    next_buttons: tuple[tuple[int, BotButton], ...]


def read_page(messages: Sequence[BotMessage], page: int) -> Page:
    entries: list[tuple[int, BotButton, ListEntry]] = []
    nexts: list[tuple[int, BotButton]] = []
    for message in messages:
        for row in message.buttons:
            for button in row:
                address = replies.list_address(button.text)
                if address is not None:
                    entry = ListEntry(
                        host=address[0],
                        port=address[1],
                        label_shape=replies.shape(button.text, limit=MAX_LABEL_SHAPE),
                        label_fp=replies.label_fingerprint(button.text),
                        page=page,
                        position=len(entries),
                        pressable=button.data is not None,
                        hex_words=replies.list_hex_words(button.text),
                    )
                    entries.append((message.id, button, entry))
                elif button.data is not None and replies.is_next_page(button.text):
                    nexts.append((message.id, button))
    return Page(entries=tuple(entries), next_buttons=tuple(nexts))


def _one_next(page: Page) -> tuple[int, BotButton] | None:
    """The page's single "next" button (None for none, or for several different ones)."""
    distinct = {(m, b.data) for m, b in page.next_buttons}
    return page.next_buttons[0] if len(distinct) == 1 else None


@dataclass(frozen=True)
class WalkStuck:
    """The walk stopped: the bot showed something it does not expect. ``screens``: what it saw."""

    step: Step
    reason: str
    screens: tuple[Screen, ...]


@dataclass(frozen=True)
class ListRead:
    """The proxies of /myproxies, in order. ``complete``: every page was read (``note`` says why
    not)."""

    entries: tuple[ListEntry, ...]
    pages: int
    complete: bool
    note: str | None
    screens: tuple[Screen, ...]


MYPROXIES: Final = Send(text="/myproxies", shown="/myproxies", then=Step.SENT_MYPROXIES)


class ListWalk:
    """``/myproxies`` and every page of it; nothing is pressed but a strict "next page" button."""

    def __init__(self, max_pages: int = MAX_PAGES) -> None:
        self.max_pages = max_pages
        self.entries: list[ListEntry] = []
        self.screens: list[Screen] = []
        self.pages: list[tuple[str, ...]] = []

    def first(self) -> Send:
        return MYPROXIES

    def _done(self, *, complete: bool, note: str | None = None) -> ListRead:
        return ListRead(
            entries=tuple(self.entries),
            pages=len(self.pages),
            complete=complete,
            note=note,
            screens=tuple(self.screens),
        )

    def next(self, step: Step, seen: Observation) -> Press | ListRead | WalkStuck:
        number = len(self.pages) + 1
        self.screens.append(screen_of(f"list_page_{number}", seen))
        if step is Step.SENT_MYPROXIES:
            if replies.no_proxies(seen.text):  # the account has no proxy at all (P19)
                return self._done(complete=True, note="empty")
            if not replies.is_proxy_list(seen.text):
                return WalkStuck(step=step, reason="unexpected_reply", screens=tuple(self.screens))
        elif step is not Step.PRESSED_NEXT_PAGE:
            return WalkStuck(step=step, reason="wrong_step", screens=tuple(self.screens))
        elif not seen.messages:  # only a toast: the page did not change
            return self._done(complete=False, note="next_page_did_not_change")
        page = read_page(seen.messages, number)
        key = tuple(e.label_fp for _, _, e in page.entries)
        if self.pages and key in self.pages:  # the bot went round: every page was read
            return self._done(complete=True)
        self.pages.append(key)
        self.entries.extend(e for _, _, e in page.entries)
        if not page.next_buttons:
            return self._done(complete=True)
        nxt = _one_next(page)
        if nxt is None:
            return self._done(complete=False, note="several_next_buttons")
        if len(self.pages) >= self.max_pages:
            return self._done(complete=False, note="too_many_pages")
        message_id, button = nxt
        assert button.data is not None
        return Press(message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_NEXT_PAGE)


# --- one proxy's screen, and its removal -------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The list button to open: same address and same label fingerprint. ``words``: how the proxy's
    own screens may name it (``host``, ``host:port``). ``copies_ok``: several identical buttons may
    be pressed (every copy is ours and dead)."""

    host: str
    port: int | None
    label_fp: str
    words: frozenset[str]
    copies_ok: bool = False
    live_tags: frozenset[str] = frozenset()

    @property
    def address(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class MenuSeen:
    """The proxy's screen (nothing was pressed on it): the public channels it names, whether it
    names the proxy, how many buttons have the removal shape, and ``remove_named``: there is
    exactly one, and the message that carries it names the proxy, as a removal would need (the
    operator client checks the very same message)."""

    channels: tuple[str, ...]
    names_proxy: bool
    remove_buttons: int
    remove_named: bool
    screens: tuple[Screen, ...]
    # P26: the bot names the sponsor by the channel's TITLE («Promoted channel: VPN & MTProxy |
    # پروکسی.»), not by its @name; None when the screen shows none ("n/a").
    promoted_title: str | None = None


@dataclass(frozen=True)
class Removed:
    """@MTProxybot said the proxy is deleted. ``confirmed``: a confirmation button was pressed."""

    confirmed: bool
    screens: tuple[Screen, ...]


def _buttons(seen: Observation, match: Any) -> list[tuple[BotMessage, BotButton]]:
    """Pressable buttons that match, with the message that carries each."""
    return [
        (message, button)
        for message in seen.messages
        for row in message.buttons
        for button in row
        if button.data is not None and match(button.text)
    ]


def _distinct(found: Sequence[tuple[BotMessage, BotButton]]) -> set[tuple[int, bytes | None]]:
    return {(m.id, b.data) for m, b in found}


class MenuWalk:
    """``/myproxies`` -> (pages) -> the target's button -> its screen; with ``remove``, then its
    removal button and the confirmation (module docstring)."""

    def __init__(self, target: Target, *, remove: bool, max_pages: int = MAX_PAGES) -> None:
        self.target = target
        self.remove = remove
        self.max_pages = max_pages
        self.pages = 0
        self.screens: list[Screen] = []

    def first(self) -> Send:
        return MYPROXIES

    def _stuck(self, step: Step, reason: str) -> WalkStuck:
        return WalkStuck(step=step, reason=reason, screens=tuple(self.screens))

    def next(self, step: Step, seen: Observation) -> Press | MenuSeen | Removed | WalkStuck:
        if step in (Step.SENT_MYPROXIES, Step.PRESSED_NEXT_PAGE):
            return self._in_list(step, seen)
        text = seen.text
        words = self.target.words
        if step is Step.PRESSED_PROXY:
            self.screens.append(screen_of("proxy_screen", seen))
            if replies.names_other_proxy(text, words):
                return self._stuck(step, "menu_of_another_proxy")
            removes = _buttons(seen, replies.is_remove_button)
            if not self.remove:
                count = len(_distinct(removes))
                return MenuSeen(
                    promoted_title=replies.promoted_title(text),
                    channels=tuple(replies.shown_channels(text)),
                    names_proxy=replies.names_proxy(text, words),
                    remove_buttons=count,
                    remove_named=count == 1 and replies.names_proxy(removes[0][0].text, words),
                    screens=tuple(self.screens),
                )
            if not replies.names_proxy(text, words):
                return self._stuck(step, "menu_does_not_name_proxy")
            if replies.screen_names_any_tag(text, self.target.live_tags):
                return self._stuck(step, "menu_of_live_proxy")
            return self._press_one(
                step, removes, "remove_button", Step.PRESSED_REMOVE, "menu_does_not_name_proxy"
            )
        if step is Step.PRESSED_REMOVE and self.remove:
            self.screens.append(screen_of("after_remove", seen))
            if replies.names_other_proxy(text, words):
                return self._stuck(step, "confirm_of_another_proxy")
            confirms = _buttons(seen, replies.is_confirm_button)
            if not confirms and replies.removal_done(text):
                return Removed(confirmed=False, screens=tuple(self.screens))
            if not replies.names_proxy(text, words):
                return self._stuck(step, "confirm_does_not_name_proxy")
            return self._press_one(
                step, confirms, "confirm_button", Step.PRESSED_CONFIRM, "confirm_does_not_name_proxy"
            )
        if step is Step.PRESSED_CONFIRM and self.remove:
            self.screens.append(screen_of("after_confirm", seen))
            if replies.removal_confirmed(text) and not replies.names_other_proxy(text, words):
                return Removed(confirmed=True, screens=tuple(self.screens))
            return self._stuck(step, "removal_not_confirmed")
        return self._stuck(step, "wrong_step")

    def _press_one(
        self, step: Step, found: list[tuple[BotMessage, BotButton]], what: str, then: Step, unnamed: str
    ) -> Press | WalkStuck:
        """The one removal-type button, only under a message that itself names the proxy (what
        the operator client's allowlist checks too)."""
        distinct = _distinct(found)
        if not distinct:
            return self._stuck(step, f"no_{what}")
        if len(distinct) > 1:
            return self._stuck(step, f"several_{what}s")
        message, button = found[0]
        if not replies.names_proxy(message.text, self.target.words):
            return self._stuck(step, unnamed)
        assert button.data is not None
        return Press(message_id=message.id, data=button.data, label=button.text, then=then)

    def _in_list(self, step: Step, seen: Observation) -> Press | WalkStuck:
        self.pages += 1
        self.screens.append(screen_of(f"list_page_{self.pages}", seen))
        if step is Step.SENT_MYPROXIES and replies.no_proxies(seen.text):
            return self._stuck(step, "proxy_not_in_list")
        if step is Step.SENT_MYPROXIES and not replies.is_proxy_list(seen.text):
            return self._stuck(step, "unexpected_reply")
        page = read_page(seen.messages, self.pages)
        target = self.target
        mine = [
            (m, b)
            for m, b, e in page.entries
            if e.pressable
            and e.label_fp == target.label_fp
            and (e.host, e.port) == (target.host, target.port)
        ]
        if mine:
            if len({(m, b.data) for m, b in mine}) > 1 and not target.copies_ok:
                return self._stuck(step, "proxy_listed_twice")
            message_id, button = mine[0]
            assert button.data is not None
            return Press(message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_PROXY)
        nxt = _one_next(page)
        if nxt is not None and self.pages < self.max_pages:
            message_id, button = nxt
            assert button.data is not None
            return Press(
                message_id=message_id, data=button.data, label=button.text, then=Step.PRESSED_NEXT_PAGE
            )
        return self._stuck(step, "proxy_not_in_list")


# --- our rows behind each entry --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryFacts:
    """What :func:`classify` needs of a stored entry."""

    host: str
    port: int | None
    label_fp: str
    tag_registration_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Mapped:
    """An entry mapped to our rows. ``regs``: the rows that decide its status (the one whose tag
    the label names, else every row at its address). ``ports``: our ports at its address.
    ``listed``: how many buttons of the list name the same address."""

    status: str
    regs: tuple[Reg, ...]
    exact: bool
    removable: bool
    ports: tuple[int, ...]
    listed: int
    note: str | None = None


# Why an entry with no dead registration of ours behind it is still ``gone`` (P18):
OLD_TAG: Final = "old_tag"  # a live address, but the label names an older registration's tag
NO_RECORD: Final = "no_record"  # our address, its rows deleted, and no node holds it now
_NODE_LABEL: Final = re.compile(r"^(slot[0-9]{1,4}|t[0-9a-f]{8})$")


@dataclass(frozen=True)
class Known:
    """What else the panel knows of addresses, for entries whose rows were deleted by hand (P16,
    P18). Every entry of the second account's list is a proxy WE registered (D37); the question is
    only whether one of our nodes still uses it.

    ``ips``: every IPv4 one of our servers ever had (``ip_history``). ``zones``: our pool domains
    (Cloudflare zones); a node's own names are ``slot<N>.<zone>`` and ``t<8 hex>.<zone>``.
    ``held``: every address a node that is not deleted holds now (its IPv4 and its names) and every
    address of a registration that is not retired."""

    ips: frozenset[str] = frozenset()
    zones: frozenset[str] = frozenset()
    held: frozenset[str] = frozenset()

    def ours_and_free(self, host: str) -> bool:
        host = host.lower()
        if host in self.held:
            return False
        if host in self.ips:
            return True
        label, _, zone = host.partition(".")
        return zone in self.zones and bool(_NODE_LABEL.fullmatch(label))


def node_gone(reg: Reg, nodes: dict[uuid.UUID, str]) -> bool:
    """A retired registration whose node was deleted (re-read from ``nodes``; a node row that no
    longer exists counts as deleted). Without a node in its key, only the reasons that mean "node
    deleted" count."""
    if reg.state != RETIRED:
        return False
    node = node_of(reg.proxy_key)
    if node is not None:
        return nodes.get(node, "deleted") in GONE_NODE_STATES
    return reg.state_reason in GONE_REASONS


def _at(entry: EntryFacts, reg: Reg) -> bool:
    return reg.host.lower() == entry.host and (entry.port is None or reg.port == entry.port)


def classify(
    entries: Sequence[EntryFacts],
    regs: Sequence[Reg],
    nodes: dict[uuid.UUID, str],
    known: Known | None = None,
) -> list[Mapped]:
    """Each entry's status and whether it may be removed (module docstring). A ``gone`` entry is
    removable when the label names its own (dead) registration's tag, or when the list names its
    address no more often than we have dead registrations there.

    Two more kinds are ``gone`` and removable (P18), each with a ``note``:

    * :data:`OLD_TAG`: a live registration uses the address, another button of the list names that
      registration's tag, and this button names none of our tags. Hetzner gave the address of a
      deleted node to a new one; this is the deleted node's registration. (Every live registration
      at the address must have its own button, or nothing here is removable.)
    * :data:`NO_RECORD`: nothing of ours is recorded at the address (its rows were deleted by hand),
      the address is ours (:meth:`Known.ours_and_free`: an IPv4 one of our servers had, or a node
      name on our domain) and no node and no registration holds it now.

    The removal itself still checks the proxy's screen: it must not show a live tag
    (:class:`MenuWalk`)."""
    known = known or Known()
    listed: dict[tuple[str, int | None], int] = {}
    for e in entries:
        listed[(e.host, e.port)] = listed.get((e.host, e.port), 0) + 1
    labelled: dict[tuple[str, int | None], set[uuid.UUID]] = {}
    for e in entries:
        if e.tag_registration_id is not None:
            labelled.setdefault((e.host, e.port), set()).add(e.tag_registration_id)
    out: list[Mapped] = []
    for e in entries:
        at = [r for r in regs if _at(e, r)]
        exact = [
            r for r in at if r.registration_id is not None and r.registration_id == e.tag_registration_id
        ]
        deciding = exact or at
        ports = tuple(sorted({r.port for r in at}))
        times = listed[(e.host, e.port)]
        if not deciding:
            free = known.ours_and_free(e.host)
            out.append(
                Mapped(GONE if free else UNKNOWN, (), False, free, ports, times, NO_RECORD if free else None)
            )
            continue
        live_here = [r for r in at if r.state != RETIRED]
        if (
            not exact
            and e.tag_registration_id is None
            and live_here
            and all(
                r.registration_id is not None and r.registration_id in labelled.get((e.host, e.port), set())
                for r in live_here
            )
        ):
            out.append(Mapped(GONE, tuple(live_here), False, True, ports, times, OLD_TAG))
            continue
        if any(r.state != RETIRED for r in deciding):
            status = LIVE
        elif all(node_gone(r, nodes) for r in deciding):
            status = GONE
        else:
            status = RETIRED_ALIVE
        dead_here = sum(1 for r in at if node_gone(r, nodes))
        removable = status == GONE and (bool(exact) or times <= dead_here)
        out.append(Mapped(status, tuple(deciding), bool(exact), removable, ports, times))
    return out


def target_of(entry: EntryFacts, mapped: Mapped, *, copies: int = 1, live_tags: Iterable[str] = ()) -> Target:
    """The walk's target for an entry: its screens may name ``host`` or ``host:port`` for the
    entry's own port, else for our ports at that address. ``live_tags``: the tags of our live
    registrations; a removal stops on a screen that shows one."""
    ports: Iterable[int] = (entry.port,) if entry.port is not None else mapped.ports
    return Target(
        host=entry.host,
        port=entry.port,
        label_fp=entry.label_fp,
        words=replies.address_words(entry.host, ports),
        copies_ok=copies > 1 and mapped.removable,
        live_tags=frozenset(live_tags),
    )
