"""@MTProxybot's own /myproxies list, a proxy's screen and the removal of a dead proxy, as DATA
(decision D10: nothing imitates the bot; the pure state machines get reply shapes as data).

What is known from real traffic is kept as ``REAL_*``: the list header (TelegramMessenger/MTProxy
issue #112), the "<host> <hex>" shape of a list button (P13 on the panel server) and the "Set
promotion" button (telemt docs/FAQ.en.md). The bot's pagination, a proxy's screen text, its removal
button and the confirmation have NEVER been published or seen: the ``ASSUMED_*`` shapes below only
check the strict matching; the first "observe" run on the server records the true ones, and the
removal stays off until they were checked (proxyhub/sponsor/inventory.py).
"""

from __future__ import annotations

import secrets
import uuid

import pytest
from test_sponsor_units import NODE_A, NODE_B, reg

from proxyhub.mtproxy.userclient import BotButton, BotMessage
from proxyhub.sponsor import botlist, replies
from proxyhub.sponsor.botlist import (
    GONE,
    LIVE,
    NO_RECORD,
    OLD_TAG,
    RETIRED_ALIVE,
    UNKNOWN,
    EntryFacts,
    Known,
    ListRead,
    ListWalk,
    MenuSeen,
    MenuWalk,
    Removed,
    Target,
    WalkStuck,
    classify,
)
from proxyhub.sponsor.conversation import Observation, Press, Send, Step
from proxyhub.sponsor.store import PENDING, REGISTERED, RETIRED

REAL_LIST_HEADER = "Here is the list of all proxies you created:"
REAL_LABEL = "t7aae9f0c.ipoxxo.online 4f1c9a0b"  # P13: "<host> <hex>"
REAL_IP_LABEL = "91.107.189.145 9e8d7c6b"
REAL_SET_PROMOTION = "Set promotion"

# --- never seen (assumed): only the strict matching is checked here ----------------------------------------
ASSUMED_NEXT = "»"
ASSUMED_BACK = "« Back"
ASSUMED_REMOVE = "🗑 Delete proxy"
ASSUMED_CONFIRM = "Yes, delete"
ASSUMED_NO = "No"
ASSUMED_MENU = "Proxy t7aae9f0c.ipoxxo.online:8443\nPromoted channel: @ipoxxo\nStats: 120 users"
ASSUMED_CONFIRM_TEXT = "Are you sure you want to delete t7aae9f0c.ipoxxo.online:8443?"
ASSUMED_DELETED = "Proxy t7aae9f0c.ipoxxo.online:8443 has been deleted."

HOST = "t7aae9f0c.ipoxxo.online"
PORT = 8443
WORDS = replies.address_words(HOST, [PORT])


def btn(label: str, data: bytes | None = None) -> BotButton:
    return BotButton(label, data if data is not None else f"cb:{label}".encode())


def msg(text: str, *rows: tuple[BotButton, ...], mid: int = 300) -> BotMessage:
    return BotMessage(id=mid, outgoing=False, text=text, edit_date=None, buttons=tuple(rows))


def seen(*messages: BotMessage, answer: str | None = None) -> Observation:
    return Observation(messages=tuple(messages), answer=answer)


def listing(*labels: str, nxt: str | None = None, header: str = REAL_LIST_HEADER) -> Observation:
    rows = [(btn(label),) for label in labels]
    if nxt is not None:
        rows.append((btn(ASSUMED_BACK), btn(nxt)))
    return seen(msg(header, *rows))


def target(label: str = REAL_LABEL, *, copies_ok: bool = False) -> Target:
    address = replies.list_address(label)
    assert address is not None
    return Target(
        host=address[0],
        port=address[1],
        label_fp=replies.label_fingerprint(label),
        words=replies.address_words(address[0], [PORT]),
        copies_ok=copies_ok,
    )


# --- the parsers --------------------------------------------------------------------------------------------


def test_a_list_button_names_one_address_and_nothing_but_hex() -> None:
    assert replies.list_address(REAL_LABEL) == (HOST, None)
    assert replies.list_address(REAL_IP_LABEL) == ("91.107.189.145", None)
    assert replies.list_address("🌐 1.2.3.4:443") == ("1.2.3.4", 443)
    assert replies.list_address(f"{HOST}:8443 4f1c 9a0b") == (HOST, 8443)
    for label in (
        "🗑 Delete 1.2.3.4",  # a word that is not hex
        "1.2.3.4 4f1c remove",
        "1.2.3.4 5.6.7.8",  # two addresses
        "1.2.3.4 1a 2b 3c",  # three hex words
        REAL_LIST_HEADER,
        ASSUMED_NEXT,
        "e.g 4f1c",  # not an address (last label one letter)
        "3.5 4f1c",
        "1.2.3.4:99999",
        "",
    ):
        assert replies.list_address(label) is None, label
    assert replies.list_hex_words(REAL_LABEL) == ("4f1c9a0b",)
    # The fingerprint finds the same button again; the label's hex is not in it.
    fp = replies.label_fingerprint(REAL_LABEL)
    assert fp == replies.label_fingerprint(" 🌐 T7AAE9F0C.ipoxxo.online   4f1c9a0b ")
    assert fp != replies.label_fingerprint(f"{HOST} 4f1c9a0c") and "4f1c9a0b" not in fp


@pytest.mark.parametrize(
    "label", ["»", "›", ">", ">>", "→", "➡️", "➡", "▶️", "⏩", "Next", "next page", "Next »", "» Next", "More"]
)
def test_a_next_page_button_has_one_of_the_strict_shapes(label: str) -> None:
    assert replies.is_next_page(label)


@pytest.mark.parametrize(
    "label", ["« Back", "«", "Prev", "2/3", "Next proxy", "»»»", "Delete", REAL_LABEL, ""]
)
def test_anything_else_is_not_a_next_page_button(label: str) -> None:
    assert not replies.is_next_page(label)


@pytest.mark.parametrize(
    "label", ["Delete", "Remove", "Delete proxy", "remove proxy", "🗑 Delete", "🗑️ Delete proxy"]
)
def test_the_removal_button_shapes(label: str) -> None:
    assert replies.is_remove_button(label) and not replies.is_confirm_button(label)


@pytest.mark.parametrize(
    "label", ["Yes", "yes", "Yes, delete", "Yes delete", "Yes, delete it", "Yes, remove", "Confirm", "✅ Yes"]
)
def test_the_confirmation_button_shapes(label: str) -> None:
    assert replies.is_confirm_button(label) and not replies.is_remove_button(label)


@pytest.mark.parametrize(
    "label",
    [
        "Delete promotion",
        "Remove promotion",
        "Delete all proxies",
        "Delete 1.2.3.4",
        "❌ Delete",
        "Yes, I'm sure",
        "Yes, delete all",
        "Delete?",
        "No",
        "Cancel",
        REAL_SET_PROMOTION,
        REAL_LABEL,
    ],
)
def test_nothing_else_is_a_removal_or_confirmation_button(label: str) -> None:
    assert not replies.is_remove_button(label) and not replies.is_confirm_button(label)


def test_the_sponsor_channel_a_screen_names() -> None:
    assert replies.shown_channels(ASSUMED_MENU) == ["ipoxxo"]
    assert replies.shown_channels("Promoted channel: https://t.me/IpoXXo") == ["ipoxxo"]
    assert replies.shown_channels("Sponsor: t.me/customer_ch and @ipoxxo") == ["customer_ch", "ipoxxo"]
    # The bot itself, a proxy link, an invite link and an e-mail are not channels.
    assert (
        replies.shown_channels(
            "@MTProxybot https://t.me/proxy?server=1.2.3.4&port=443&secret=ab "
            "t.me/+AbCdEf t.me/joinchat/AAA a@b.cc"
        )
        == []
    )
    assert replies.shown_channels("No promotion set.") == []


def test_removal_done_needs_the_word_and_no_doubt() -> None:
    for text in (ASSUMED_DELETED, "Done! Proxy removed.", "The proxy was successfully deleted"):
        assert replies.removal_done(text), text
    for text in (
        ASSUMED_CONFIRM_TEXT,
        "Proxy not deleted: error",
        "Sorry, this proxy cannot be removed.",
        "Really delete it? It will be removed.",
        "Deleting...",
        "Proxy deleted. Confirm?",
        "Hmm.",
    ):
        assert not replies.removal_done(text), text


def test_a_screen_shape_hides_addresses_hex_links_and_secrets() -> None:
    secret, tag = secrets.token_hex(16), secrets.token_hex(16)
    raw = (
        f"Proxy {HOST}:8443 ({REAL_IP_LABEL})\nTag: {tag}\nSecret: ee{secret}6578616d706c652e636f6d\n"
        f"Link: tg://proxy?server={HOST}&port=8443&secret={secret}\nPromoted: @ipoxxo, https://t.me/ipoxxo"
    )
    shaped = replies.shape(raw)
    assert shaped.splitlines() == [
        "Proxy <host:port> (<host> <hex>)",
        "Tag: [hex]",
        "Secret: [hex]",
        "Link: <link>",
        "Promoted: @ipoxxo, https://t.me/ipoxxo",
    ]
    for leaked in (secret, tag, HOST, "91.107", "4f1c", "9e8d"):
        assert leaked not in shaped
    assert replies.shape("🗑 Delete proxy") == "🗑 Delete proxy"
    assert replies.shape("Users: 1203 today") == "Users: 1203 today"
    assert replies.shape("key " + "Zq1" * 8) == "key <token>"
    assert len(replies.shape("word " * 200)) == replies.MAX_SHAPE
    assert "[token]" in replies.scrub("secret ZW5jb2RlZDEyMzQ1Njc4OTA") and "1.2.3.4" in replies.scrub(
        "1.2.3.4"
    )


def test_a_label_names_our_tag_only_unambiguously() -> None:
    tag_a, tag_b = "4f1c9a0b" + "0" * 23 + "1", "77" * 16
    assert replies.tag_hint(("4f1c9a0b",), [tag_a, tag_b]) == tag_a
    assert replies.tag_hint(("aa0001",), ["aa0001" + "1" * 26, "2" * 26 + "aa0001"]) is None  # two match
    assert replies.tag_hint(("4f1c",), [tag_a]) is None  # too short to tell
    assert replies.tag_hint((), [tag_a]) is None


# --- the list walk ------------------------------------------------------------------------------------------


def test_the_list_walk_reads_every_page_and_presses_only_next() -> None:
    walk = ListWalk()
    assert walk.first() == Send(text="/myproxies", shown="/myproxies", then=Step.SENT_MYPROXIES)
    page1 = listing(REAL_LABEL, REAL_IP_LABEL, nxt=ASSUMED_NEXT)
    press = walk.next(Step.SENT_MYPROXIES, page1)
    assert press == Press(
        message_id=300, data=b"cb:\xc2\xbb", label=ASSUMED_NEXT, then=Step.PRESSED_NEXT_PAGE
    )
    page2 = seen(msg("Page 2", (btn("1.2.3.4:443 aa01"),), (btn("« Prev"),)))  # the same message, edited
    done = walk.next(Step.PRESSED_NEXT_PAGE, page2)
    assert isinstance(done, ListRead) and done.complete and done.pages == 2 and done.note is None
    assert [(e.address, e.page, e.position) for e in done.entries] == [
        (HOST, 1, 0),
        ("91.107.189.145", 1, 1),
        ("1.2.3.4:443", 2, 0),
    ]
    assert [s.stage for s in done.screens] == ["list_page_1", "list_page_2"]
    assert done.screens[0].buttons == (("<host> <hex>",), ("<host> <hex>",), ("« Back", "»"))
    assert "4f1c" not in repr(done) and "9e8d" not in repr(done)  # the hex words are never shown


def test_a_single_page_list_is_complete_at_once() -> None:
    done = ListWalk().next(Step.SENT_MYPROXIES, listing(REAL_LABEL, REAL_SET_PROMOTION))
    assert isinstance(done, ListRead) and done.complete and done.pages == 1 and len(done.entries) == 1


@pytest.mark.parametrize(
    ("pages", "complete", "note"),
    [
        # the bot went round to the first page again: every page was read
        (
            [listing(REAL_LABEL, nxt="»"), listing(REAL_IP_LABEL, nxt="»"), listing(REAL_LABEL, nxt="»")],
            True,
            None,
        ),
        # two different "next" buttons: which one is unknown
        (
            [seen(msg(REAL_LIST_HEADER, (btn(REAL_LABEL),), (btn("»"), btn("Next"))))],
            False,
            "several_next_buttons",
        ),
        # the press changed nothing (only a toast)
        (
            [listing(REAL_LABEL, nxt="»"), seen(answer="This is the last page")],
            False,
            "next_page_did_not_change",
        ),
    ],
)
def test_the_list_walk_stops_when_paging_is_not_clear(
    pages: list[Observation], complete: bool, note: str | None
) -> None:
    walk = ListWalk()
    result: object = None
    step = Step.SENT_MYPROXIES
    for page in pages:
        result = walk.next(step, page)
        step = Step.PRESSED_NEXT_PAGE
    assert isinstance(result, ListRead) and (result.complete, result.note) == (complete, note)


def test_the_list_walk_reads_at_most_max_pages() -> None:
    walk = ListWalk(max_pages=3)
    step, result = Step.SENT_MYPROXIES, None
    for i in range(3):
        result = walk.next(step, listing(f"10.0.0.{i + 1} aa", nxt="»"))
        step = Step.PRESSED_NEXT_PAGE
    assert isinstance(result, ListRead) and not result.complete and result.note == "too_many_pages"
    assert result.pages == 3 and len(result.entries) == 3


def test_the_list_walk_stops_on_anything_but_the_list() -> None:
    stuck = ListWalk().next(Step.SENT_MYPROXIES, seen(msg("You have no proxies yet.")))
    assert isinstance(stuck, WalkStuck) and stuck.reason == "unexpected_reply" and stuck.screens
    assert isinstance(ListWalk().next(Step.PRESSED_PROXY, listing(REAL_LABEL)), WalkStuck)


# --- a proxy's screen: the promotion check and the "observe" run --------------------------------------------


def menu(text: str = ASSUMED_MENU, *rows: tuple[BotButton, ...]) -> Observation:
    rows = rows or ((btn(REAL_SET_PROMOTION), btn(ASSUMED_REMOVE)), (btn(ASSUMED_BACK),))
    return seen(msg(text, *rows))


def test_looking_opens_the_target_on_its_page_and_presses_nothing_on_its_screen() -> None:
    walk = MenuWalk(target(), remove=False)
    assert walk.first().text == "/myproxies"
    nxt = walk.next(Step.SENT_MYPROXIES, listing("1.2.3.4 aa", nxt="»"))
    assert isinstance(nxt, Press) and nxt.then is Step.PRESSED_NEXT_PAGE
    pick = walk.next(Step.PRESSED_NEXT_PAGE, listing(REAL_IP_LABEL, REAL_LABEL))
    assert pick == Press(
        message_id=300, data=f"cb:{REAL_LABEL}".encode(), label=REAL_LABEL, then=Step.PRESSED_PROXY
    )
    result = walk.next(Step.PRESSED_PROXY, menu())
    assert isinstance(result, MenuSeen)
    assert (result.channels, result.names_proxy, result.remove_buttons, result.remove_named) == (
        ("ipoxxo",),
        True,
        1,
        True,
    )
    assert [s.stage for s in result.screens] == ["list_page_1", "list_page_2", "proxy_screen"]
    assert result.screens[-1].text == "Proxy <host:port>\nPromoted channel: @ipoxxo\nStats: 120 users"
    assert result.screens[-1].buttons == (("Set promotion", "🗑 Delete proxy"), ("« Back",))


def test_looking_reports_a_screen_without_channel_or_name_and_counts_removal_buttons() -> None:
    walk = MenuWalk(target(), remove=False)
    walk.next(Step.SENT_MYPROXIES, listing(REAL_LABEL))
    result = walk.next(Step.PRESSED_PROXY, menu("Proxy settings", (btn(REAL_SET_PROMOTION),)))
    assert isinstance(result, MenuSeen)
    assert (result.channels, result.names_proxy, result.remove_buttons, result.remove_named) == (
        (),
        False,
        0,
        False,
    )


def test_a_removal_button_counts_only_under_a_message_that_names_the_proxy() -> None:
    """The address in ANOTHER message (or the toast) is not enough: the operator client checks the
    message that carries the button, and so do the walks."""
    split = seen(
        msg(ASSUMED_MENU, mid=300),
        msg("Actions:", (btn(REAL_SET_PROMOTION), btn(ASSUMED_REMOVE)), mid=301),
    )
    look = MenuWalk(target(), remove=False)
    look.next(Step.SENT_MYPROXIES, listing(REAL_LABEL))
    result = look.next(Step.PRESSED_PROXY, split)
    assert isinstance(result, MenuSeen)
    assert (result.names_proxy, result.remove_buttons, result.remove_named) == (True, 1, False)
    stuck = removal_walk().next(Step.PRESSED_PROXY, split)
    assert isinstance(stuck, WalkStuck) and stuck.reason == "menu_does_not_name_proxy"
    walk = removal_walk()
    walk.next(Step.PRESSED_PROXY, menu())
    confirm = seen(msg(ASSUMED_CONFIRM_TEXT, mid=302), msg("Choose:", (btn(ASSUMED_CONFIRM),), mid=303))
    stuck = walk.next(Step.PRESSED_REMOVE, confirm)
    assert isinstance(stuck, WalkStuck) and stuck.reason == "confirm_does_not_name_proxy"


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (listing(REAL_IP_LABEL), "proxy_not_in_list"),
        (listing(f"{HOST} 4f1c9a0c"), "proxy_not_in_list"),  # same host, another label: another entry
        (listing(REAL_LABEL, REAL_LABEL + " "), "proxy_listed_twice"),  # two identical buttons
        (listing(REAL_LABEL, header="Choose:"), "unexpected_reply"),
    ],
)
def test_looking_stops_when_the_target_is_not_exactly_one_button(
    observation: Observation, reason: str
) -> None:
    result = MenuWalk(target(), remove=False).next(Step.SENT_MYPROXIES, observation)
    assert isinstance(result, WalkStuck) and result.reason == reason


def test_a_screen_of_another_proxy_stops_the_walk() -> None:
    walk = MenuWalk(target(), remove=False)
    walk.next(Step.SENT_MYPROXIES, listing(REAL_LABEL))
    result = walk.next(Step.PRESSED_PROXY, menu("Proxy 5.6.7.8:443\nPromoted channel: @ipoxxo"))
    assert isinstance(result, WalkStuck) and result.reason == "menu_of_another_proxy"


# --- removal (only for a dead proxy, only once removal is on) -----------------------------------------------


def removal_walk(**kw: bool) -> MenuWalk:
    walk = MenuWalk(target(**kw), remove=True)
    assert isinstance(walk.next(Step.SENT_MYPROXIES, listing(REAL_LABEL)), Press)
    return walk


CONFIRM_SCREEN = seen(msg(ASSUMED_CONFIRM_TEXT, (btn(ASSUMED_CONFIRM), btn(ASSUMED_NO))))


def test_removal_presses_the_strict_removal_button_then_the_strict_confirmation() -> None:
    walk = removal_walk()
    press = walk.next(Step.PRESSED_PROXY, menu())
    assert press == Press(
        message_id=300, data=f"cb:{ASSUMED_REMOVE}".encode(), label=ASSUMED_REMOVE, then=Step.PRESSED_REMOVE
    )
    confirm = walk.next(Step.PRESSED_REMOVE, CONFIRM_SCREEN)
    assert confirm == Press(
        message_id=300,
        data=f"cb:{ASSUMED_CONFIRM}".encode(),
        label=ASSUMED_CONFIRM,
        then=Step.PRESSED_CONFIRM,
    )
    done = walk.next(Step.PRESSED_CONFIRM, seen(msg(ASSUMED_DELETED)))
    assert isinstance(done, Removed) and done.confirmed
    assert [s.stage for s in done.screens] == ["list_page_1", "proxy_screen", "after_remove", "after_confirm"]
    assert done.screens[2].text == "Are you sure you want to delete <host:port>?"


def test_a_bot_that_removes_at_once_is_accepted() -> None:
    walk = removal_walk()
    walk.next(Step.PRESSED_PROXY, menu())
    done = walk.next(
        Step.PRESSED_REMOVE, seen(msg(REAL_LIST_HEADER, (btn(REAL_IP_LABEL),)), answer="Proxy deleted")
    )
    assert isinstance(done, Removed) and not done.confirmed


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (menu("Proxy settings"), "menu_does_not_name_proxy"),
        (menu(ASSUMED_MENU, (btn(REAL_SET_PROMOTION), btn("Delete promotion"))), "no_remove_button"),
        (menu(ASSUMED_MENU, (btn(REAL_SET_PROMOTION),)), "no_remove_button"),
        (menu(ASSUMED_MENU, (btn("Delete"), btn("Remove proxy"))), "several_remove_buttons"),
        (menu(ASSUMED_MENU, (BotButton(ASSUMED_REMOVE, None),)), "no_remove_button"),  # not a callback
        (menu(f"Proxy {HOST}:8443 and 5.6.7.8:443"), "menu_of_another_proxy"),
    ],
)
def test_removal_stops_on_any_unexpected_proxy_screen(screen: Observation, reason: str) -> None:
    result = removal_walk().next(Step.PRESSED_PROXY, screen)
    assert isinstance(result, WalkStuck) and result.reason == reason and result.step is Step.PRESSED_PROXY


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (seen(msg("Are you sure?", (btn(ASSUMED_CONFIRM), btn(ASSUMED_NO)))), "confirm_does_not_name_proxy"),
        (seen(msg("Delete 5.6.7.8:443?", (btn(ASSUMED_CONFIRM),))), "confirm_of_another_proxy"),
        (seen(msg(ASSUMED_CONFIRM_TEXT, (btn("Yes, I'm sure"), btn(ASSUMED_NO)))), "no_confirm_button"),
        (seen(msg(ASSUMED_CONFIRM_TEXT, (btn("Yes"), btn("Confirm")))), "several_confirm_buttons"),
        (seen(msg(ASSUMED_CONFIRM_TEXT, (btn(ASSUMED_NO),))), "no_confirm_button"),
    ],
)
def test_removal_never_confirms_an_unexpected_confirmation(screen: Observation, reason: str) -> None:
    walk = removal_walk()
    walk.next(Step.PRESSED_PROXY, menu())
    result = walk.next(Step.PRESSED_REMOVE, screen)
    assert isinstance(result, WalkStuck) and result.reason == reason and result.step is Step.PRESSED_REMOVE


@pytest.mark.parametrize(
    "answer",
    ["Error: try again later", "Are you sure?", "Proxy not found.", "Done.", "Proxy 5.6.7.8:443 deleted"],
)
def test_a_confirmation_without_a_clear_deleted_is_not_a_removal(answer: str) -> None:
    walk = removal_walk()
    walk.next(Step.PRESSED_PROXY, menu())
    walk.next(Step.PRESSED_REMOVE, CONFIRM_SCREEN)
    result = walk.next(Step.PRESSED_CONFIRM, seen(msg(answer)))
    assert isinstance(result, WalkStuck) and result.reason == "removal_not_confirmed"


def test_identical_dead_buttons_may_be_removed_one_by_one_but_never_looked_at() -> None:
    twice = seen(msg(REAL_LIST_HEADER, (BotButton(REAL_LABEL, b"p:1"),), (BotButton(REAL_LABEL, b"p:2"),)))
    assert isinstance(MenuWalk(target(), remove=True).next(Step.SENT_MYPROXIES, twice), WalkStuck)
    pick = MenuWalk(target(copies_ok=True), remove=True).next(Step.SENT_MYPROXIES, twice)
    assert isinstance(pick, Press) and pick.then is Step.PRESSED_PROXY


def test_the_walks_never_take_a_step_out_of_order() -> None:
    look = MenuWalk(target(), remove=False)
    look.next(Step.SENT_MYPROXIES, listing(REAL_LABEL))
    assert isinstance(look.next(Step.PRESSED_REMOVE, CONFIRM_SCREEN), WalkStuck)  # looking never removes
    assert isinstance(look.next(Step.PRESSED_CONFIRM, seen(msg(ASSUMED_DELETED))), WalkStuck)
    assert isinstance(look.next(Step.SENT_SECRET, menu()), WalkStuck)


# --- our rows behind each entry -----------------------------------------------------------------------------


def facts(
    host: str = HOST, port: int | None = None, tag_reg: uuid.UUID | None = None, fp: str = "f1"
) -> EntryFacts:
    return EntryFacts(host=host, port=port, label_fp=fp, tag_registration_id=tag_reg)


def test_each_entry_maps_to_live_gone_retired_or_unknown() -> None:
    live = reg(REGISTERED, host=HOST, node=NODE_A)
    dead = reg(RETIRED, host="5.6.7.8", node=NODE_B, state_reason="node_gone")
    moved = reg(RETIRED, host="9.9.9.9", node=None, state_reason="address_changed")
    candidate = reg("pending", host="7.7.7.7")
    nodes = {NODE_A: "ready", NODE_B: "deleted"}
    got = classify(
        [facts(), facts("5.6.7.8"), facts("9.9.9.9"), facts("8.8.8.8"), facts("7.7.7.7"), facts(HOST, 443)],
        [live, dead, moved, candidate],
        nodes,
    )
    assert [m.status for m in got] == [LIVE, GONE, RETIRED_ALIVE, UNKNOWN, LIVE, UNKNOWN]
    assert [m.removable for m in got] == [False, True, False, False, False, False]
    assert got[1].ports == (8443,) and got[1].listed == 1


def test_a_retired_registration_counts_as_gone_only_when_its_node_is_deleted_now() -> None:
    dead = reg(RETIRED, host="5.6.7.8", node=NODE_B, state_reason="node_gone")
    assert classify([facts("5.6.7.8")], [dead], {NODE_B: "ready"})[0].status == RETIRED_ALIVE  # it came back?
    assert classify([facts("5.6.7.8")], [dead], {})[0].status == GONE  # the node row itself is gone
    no_key = reg(RETIRED, host="5.6.7.8", node=None, state_reason="proxy_gone")
    assert classify([facts("5.6.7.8")], [no_key], {})[0].removable


def test_a_reused_slot_name_is_decided_by_the_tag_the_label_names() -> None:
    """slot3.example.com served a node that was deleted and now serves a new one: the list holds two
    buttons with the same host. Without a tag match neither may go; the one whose label names the
    dead registration's tag may."""
    host = "slot3.example.com"
    old = reg(RETIRED, host=host, node=NODE_B, state_reason="node_gone")
    new = reg(REGISTERED, host=host, node=NODE_A)
    nodes = {NODE_A: "ready", NODE_B: "deleted"}
    plain = classify([facts(host, fp="a"), facts(host, fp="b")], [old, new], nodes)
    assert [(m.status, m.removable) for m in plain] == [(LIVE, False), (LIVE, False)]
    exact = classify(
        [facts(host, tag_reg=old.registration_id, fp="a"), facts(host, tag_reg=new.registration_id, fp="b")],
        [old, new],
        nodes,
    )
    assert [(m.status, m.removable, m.exact) for m in exact] == [(GONE, True, True), (LIVE, False, True)]


def test_more_buttons_than_dead_registrations_at_an_address_are_never_removed() -> None:
    dead = reg(RETIRED, host="5.6.7.8", node=NODE_B, state_reason="node_gone")
    two = classify([facts("5.6.7.8", fp="a"), facts("5.6.7.8", fp="b")], [dead], {})
    assert [m.removable for m in two] == [False, False] and two[0].listed == 2
    dead2 = reg(RETIRED, host="5.6.7.8", node=None, state_reason="proxy_gone")
    assert [
        m.removable for m in classify([facts("5.6.7.8", fp="a"), facts("5.6.7.8", fp="b")], [dead, dead2], {})
    ] == [
        True,
        True,
    ]


def test_the_walk_target_names_the_proxy_by_its_known_ports() -> None:
    dead = reg(RETIRED, host="5.6.7.8", node=NODE_B, state_reason="node_gone")
    entry = facts("5.6.7.8", fp=replies.label_fingerprint("5.6.7.8 aa"))
    walk_target = botlist.target_of(entry, classify([entry], [dead], {})[0])
    assert walk_target.words == frozenset({"5.6.7.8", "5.6.7.8:8443"}) and not walk_target.copies_ok
    with_port = facts("5.6.7.8", 443)
    assert botlist.target_of(with_port, classify([with_port], [dead], {})[0]).words == frozenset(
        {"5.6.7.8", "5.6.7.8:443"}
    )


# --- P18: registrations whose rows are gone ---------------------------------------------------------------


def test_an_older_registration_of_a_live_address_goes_once_ours_has_its_own_button() -> None:
    """P18: Hetzner gave the deleted node's 65.109.209.24 to the new node; the list holds both
    registrations. The one whose label names our live tag stays; the other is the old one."""
    host = "65.109.209.24"
    live = reg(REGISTERED, host=host, node=NODE_A, ad_tag="9e8d7c6b" + "0" * 24)
    nodes = {NODE_A: "ready"}
    ours, old = facts(host, tag_reg=live.registration_id, fp="ours"), facts(host, fp="old")
    got = classify([ours, old], [live], nodes)
    assert [(m.status, m.removable, m.note) for m in got] == [(LIVE, False, None), (GONE, True, OLD_TAG)]
    # Without a button of our own, or with a proxy still waiting for its tag there, nothing goes.
    assert [(m.status, m.removable) for m in classify([old], [live], nodes)] == [(LIVE, False)]
    waiting = reg(PENDING, host=host, node=NODE_B)
    assert [m.removable for m in classify([ours, old], [live, waiting], nodes)] == [False, False]
    # Our button at ANOTHER port of the same host does not vouch for this one.
    elsewhere = facts(host, 443, tag_reg=live.registration_id, fp="ours")
    assert classify([elsewhere, facts(host, 8443, fp="old")], [live], nodes)[1].removable is False


def test_an_address_of_ours_with_no_rows_left_goes_only_when_nothing_holds_it() -> None:
    known = Known(
        ips=frozenset({"5.161.131.202", "65.109.209.24"}),
        zones=frozenset({"ipoxxo.online"}),
        held=frozenset({"65.109.209.24", "t0a1b2c3d.ipoxxo.online"}),
    )
    cases = {
        "5.161.131.202": (GONE, True, NO_RECORD),  # an IPv4 one of our servers had
        "te145a554.ipoxxo.online": (GONE, True, NO_RECORD),  # a test node's name on our domain
        "slot3.ipoxxo.online": (GONE, True, NO_RECORD),  # a slot name on our domain
        "65.109.209.24": (UNKNOWN, False, None),  # a node holds it now
        "t0a1b2c3d.ipoxxo.online": (UNKNOWN, False, None),
        "8.8.8.8": (UNKNOWN, False, None),  # never ours
        "www.ipoxxo.online": (UNKNOWN, False, None),  # our domain, but not a node's name
        "te145a554.example.com": (UNKNOWN, False, None),  # a node's shape on another domain
    }
    got = classify([facts(h, fp=h) for h in cases], [], {}, known)
    assert [(m.status, m.removable, m.note) for m in got] == list(cases.values())
    # Without what the panel knows (the default), nothing changes: unknown stays unknown.
    assert [m.status for m in classify([facts("5.161.131.202")], [], {})] == [UNKNOWN]


def test_a_removal_never_presses_delete_on_a_screen_that_shows_a_live_tag() -> None:
    live_tag = "9e8d7c6b" + "0" * 24
    guarded = Target(
        host=HOST,
        port=PORT,
        label_fp=replies.label_fingerprint(REAL_LABEL),
        words=WORDS,
        live_tags=frozenset({live_tag}),
    )
    rows = ((btn(REAL_SET_PROMOTION), btn("Stats")), (btn("Delete proxy"), btn("« Back to list")))
    for tag, expect in ((live_tag, "stuck"), ("5f5f5f5f" + "e" * 24, "press")):
        walk = MenuWalk(guarded, remove=True)
        walk.next(Step.SENT_MYPROXIES, listing(REAL_LABEL))
        screen = menu(
            f"Proxy {HOST}:{PORT} with the tag {tag}.\nPromoted channel: n/a.\nHow can I help?", *rows
        )
        result = walk.next(Step.PRESSED_PROXY, screen)
        if expect == "stuck":
            assert isinstance(result, WalkStuck) and result.reason == "menu_of_live_proxy"
        else:
            assert isinstance(result, Press) and result.label == "Delete proxy"
            assert result.then is Step.PRESSED_REMOVE
