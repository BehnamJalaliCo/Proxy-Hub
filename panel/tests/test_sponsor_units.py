"""Pure parts of the sponsor module: campaigns and the wanted channel, the choice of the next
conversation, backoff, pacing decisions, the provider contract's shape, and how the driver tells
the bot's new or edited messages from old ones (all as data; no I/O)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from proxyhub.mtproxy.userclient import BotButton, BotMessage
from proxyhub.sponsor import campaigns as camp
from proxyhub.sponsor import pacing
from proxyhub.sponsor.campaigns import CampaignError, Window
from proxyhub.sponsor.conversation import PromoteJob, RegisterJob
from proxyhub.sponsor.driver import changes
from proxyhub.sponsor.provider import node_of, user_of, valid
from proxyhub.sponsor.service import PAUSE_AFTER_STUCK, backoff, choose, in_use, stuck_count
from proxyhub.sponsor.store import FAILED, PENDING, REGISTERED, RETIRED, Reg

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
LEAD = timedelta(minutes=60)


def window(
    start_h: float, end_h: float, *, state: str = camp.SCHEDULED, channel: str = "customer1"
) -> Window:
    return Window(
        id=uuid.uuid4(),
        channel=channel,
        starts_at=NOW + timedelta(hours=start_h),
        ends_at=NOW + timedelta(hours=end_h),
        state=state,
    )


# --- campaigns -----------------------------------------------------------------------------------------


def test_the_switch_to_a_customer_starts_lead_early_and_back_exactly_at_the_end() -> None:
    w = window(2, 26)
    assert camp.wanted_channel([w], "ipoxxo", NOW, LEAD) == "ipoxxo"
    assert camp.wanted_channel([w], "ipoxxo", NOW + timedelta(minutes=59), LEAD) == "ipoxxo"
    assert camp.wanted_channel([w], "ipoxxo", NOW + timedelta(hours=1), LEAD) == "customer1"  # start - lead
    assert camp.wanted_channel([w], "ipoxxo", NOW + timedelta(hours=25, minutes=59), LEAD) == "customer1"
    assert camp.wanted_channel([w], "ipoxxo", NOW + timedelta(hours=26), LEAD) == "ipoxxo"  # the end
    assert camp.wanted_channel(
        [replace(w, state=camp.CANCELLED)], "ipoxxo", NOW + timedelta(hours=3), LEAD
    ) == ("ipoxxo")
    assert camp.wanted_channel([w], "ipoxxo", NOW + timedelta(hours=1), timedelta(0)) == "ipoxxo"  # no lead


def test_back_to_back_campaigns_hand_over_at_the_boundary() -> None:
    first = window(2, 10, state=camp.ACTIVE, channel="first")
    second = window(10, 20, channel="second")
    at = NOW + timedelta(hours=9, minutes=30)  # the second's lead has begun, the first still runs
    assert camp.wanted_channel([second, first], "ipoxxo", at, LEAD) == "first"
    assert camp.wanted_channel([second, first], "ipoxxo", NOW + timedelta(hours=10), LEAD) == "second"


def test_campaign_states_move_on_time() -> None:
    w = window(2, 26)
    assert camp.transitions([w], NOW, LEAD) == []
    assert camp.transitions([w], NOW + timedelta(hours=1), LEAD) == [(w, camp.ACTIVE)]
    active = replace(w, state=camp.ACTIVE)
    assert camp.transitions([active], NOW + timedelta(hours=25), LEAD) == []
    assert camp.transitions([active], NOW + timedelta(hours=26), LEAD) == [(active, camp.FINISHED)]
    # Missed entirely (the worker was down, or the kill switch was on): straight to finished.
    assert camp.transitions([w], NOW + timedelta(hours=30), LEAD) == [(w, camp.FINISHED)]
    assert camp.transitions([replace(w, state=camp.CANCELLED)], NOW + timedelta(hours=30), LEAD) == []


def test_campaign_windows_are_checked() -> None:
    other = window(10, 20)
    ok = {"now": NOW, "others": [other]}
    camp.check_window(NOW + timedelta(hours=1), NOW + timedelta(hours=8), **ok)  # type: ignore[arg-type]
    camp.check_window(NOW + timedelta(hours=21), NOW + timedelta(hours=30), **ok)  # type: ignore[arg-type]
    # Back to back on both sides of the other campaign.
    camp.check_window(NOW + timedelta(hours=1), NOW + timedelta(hours=10), **ok)  # type: ignore[arg-type]
    camp.check_window(NOW + timedelta(hours=20), NOW + timedelta(hours=22), **ok)  # type: ignore[arg-type]
    cases = [
        (NOW + timedelta(hours=5), NOW + timedelta(hours=4), "campaign_end_before_start"),
        (NOW - timedelta(hours=5), NOW - timedelta(hours=1), "campaign_in_past"),
        (NOW - timedelta(hours=1), NOW + timedelta(hours=1), "campaign_start_in_past"),
        (NOW + timedelta(hours=1), NOW + timedelta(days=400), "campaign_too_long"),
        (NOW + timedelta(hours=5), NOW + timedelta(hours=11), "campaign_overlap"),
        (NOW + timedelta(hours=12), NOW + timedelta(hours=13), "campaign_overlap"),
        (NOW + timedelta(hours=9), NOW + timedelta(hours=21), "campaign_overlap"),
        (NOW + timedelta(hours=19, minutes=59), NOW + timedelta(hours=22), "campaign_overlap"),
        (datetime(2027, 1, 1), datetime(2027, 1, 2), "campaign_time_format"),  # naive on purpose
    ]
    for start, end, code in cases:
        with pytest.raises(CampaignError) as caught:
            camp.check_window(start, end, **ok)  # type: ignore[arg-type]
        assert caught.value.code == code, (start, end)
    # Editing a campaign does not overlap with itself; a closed one blocks nothing.
    camp.check_window(other.starts_at, other.ends_at, now=NOW, others=[other], editing=other.id)
    closed = replace(other, state=camp.FINISHED)
    camp.check_window(other.starts_at, other.ends_at, now=NOW, others=[closed])
    # An active campaign keeps its (past) start when its end is changed.
    camp.check_window(
        NOW - timedelta(hours=3), NOW + timedelta(hours=3), now=NOW, others=[], already_started=True
    )


# --- which conversation next -----------------------------------------------------------------------------


NODE_A, NODE_B = uuid.uuid4(), uuid.uuid4()


def reg(state: str, *, node: uuid.UUID | None = NODE_A, **kw: object) -> Reg:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        # A candidate (no tag yet) has no registration row; anything else has one.
        "registration_id": None if state == PENDING else uuid.uuid4(),
        "host": "1.2.3.4",
        "port": 8443,
        "secret_name": "s1",
        "ad_tag": None,
        "sponsor_channel": None,
        "state": state,
        "state_reason": None,
        "registered_at": None,
        "created_at": NOW - timedelta(days=1),
        "proxy_key": f"mtproto:{node}:s1" if node else None,
        "attempts": 0,
        "next_attempt_at": None,
        "last_error": None,
        "last_step": None,
        "stuck_reason": None,
        "stuck_hint": None,
        "stuck_at": None,
        "last_bot_text": None,
        "alerted_at": None,
        "promotion_set_at": None,
        "tag_applied_at": None,
        "tag_failed_at": None,
    }
    base.update(kw)
    return Reg(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Proxy:
    key: str
    host: str
    port: int
    secret_hex: str


SECRET = "ab" * 16
TAG = "cd" * 16
LIVE_NODES = {NODE_A: "ready", NODE_B: "ready"}


def pick(
    regs: list[Reg], *, live: dict[str, Proxy] | None = None, wanted: str = "ipoxxo", **kw: object
) -> object:
    return choose(
        regs,
        live=live or {},  # type: ignore[arg-type]
        nodes=kw.pop("nodes", LIVE_NODES),  # type: ignore[arg-type]
        wanted=wanted,
        default="ipoxxo",
        campaign_channels=kw.pop("campaign_channels", set()),  # type: ignore[arg-type]
        now=NOW,
    )


def test_a_new_proxy_is_registered_with_its_secret_from_the_provider() -> None:
    pending = reg(PENDING)
    proxy = Proxy(key=str(pending.proxy_key), host="1.2.3.4", port=8443, secret_hex=SECRET)
    work = pick([pending], live={proxy.key: proxy})
    assert work is not None and work.job == RegisterJob(host="1.2.3.4", port=8443, secret_hex=SECRET)  # type: ignore[attr-defined]
    assert SECRET not in repr(work)
    # Not listed by the provider any more (no secret to send), or listed at another address: nothing.
    assert pick([pending]) is None
    moved = Proxy(key=proxy.key, host="5.6.7.8", port=8443, secret_hex=SECRET)
    assert pick([pending], live={proxy.key: moved}) is None
    # Only a candidate is registered: a tagless row written elsewhere is never worked on, and a
    # stuck candidate waits for the owner.
    assert pick([replace(pending, registration_id=uuid.uuid4())], live={proxy.key: proxy}) is None
    assert pick([replace(pending, stuck_reason="unexpected_reply")], live={proxy.key: proxy}) is None


def test_a_registered_proxy_gets_the_wanted_sponsor_and_campaigns_come_first() -> None:
    registered = reg(REGISTERED, ad_tag=TAG, registered_at=NOW - timedelta(days=2))
    done = reg(REGISTERED, ad_tag=TAG, sponsor_channel="ipoxxo", node=NODE_B)
    work = pick([registered, done])
    # The job carries the registration's tag: it picks our button when the list names the address twice.
    assert work is not None and work.job == PromoteJob(host="1.2.3.4", port=8443, channel="ipoxxo", tag=TAG)  # type: ignore[attr-defined]
    assert work.priority == 2  # type: ignore[attr-defined]
    pending = reg(PENDING, node=NODE_B)
    proxy = Proxy(key=str(pending.proxy_key), host="1.2.3.4", port=8443, secret_hex=SECRET)
    assert pick([registered, pending], live={proxy.key: proxy}).priority == 1  # type: ignore[union-attr]
    # During a campaign every proxy not yet showing the customer is switched first (priority 0) ...
    work = pick([pending, done], live={proxy.key: proxy}, wanted="customer1", campaign_channels={"customer1"})
    assert work is not None and work.priority == 0 and work.job.channel == "customer1"  # type: ignore[attr-defined, union-attr]
    # ... and switched back first when it ends.
    on_customer = replace(done, sponsor_channel="customer1")
    work = pick([pending, on_customer], live={proxy.key: proxy}, campaign_channels={"customer1"})
    assert work is not None and work.priority == 0 and work.job.channel == "ipoxxo"  # type: ignore[attr-defined, union-attr]


def test_nothing_is_picked_for_proxies_not_in_use_not_due_or_while_paused() -> None:
    registered = reg(REGISTERED, ad_tag=TAG)
    assert pick([registered], nodes={NODE_A: "deleted"}) is None
    assert pick([registered], nodes={NODE_A: "burned"}) is None
    assert pick([registered], nodes={}) is None  # the node is unknown
    assert pick([replace(registered, next_attempt_at=NOW + timedelta(minutes=1))]) is None
    assert pick([replace(registered, next_attempt_at=NOW)]) is not None
    assert pick([reg(FAILED, ad_tag=TAG), reg(RETIRED, ad_tag=TAG)]) is None
    stuck = [
        reg(FAILED, node=uuid.uuid4(), stuck_reason="unexpected_reply") for _ in range(PAUSE_AFTER_STUCK)
    ]
    assert pick([registered, *stuck]) is None  # the bot probably says something new: wait for the owner
    assert pick([registered, *stuck[:-1]]) is not None
    # Stuck means stuck_reason (a candidate has no registration state of its own); a failed row
    # with none (written elsewhere) or a retired one never pauses anything.
    candidates = [replace(r, registration_id=None) for r in stuck]
    assert stuck_count(candidates) == PAUSE_AFTER_STUCK and pick([registered, *candidates]) is None
    quiet = [reg(FAILED, node=uuid.uuid4()), reg(RETIRED, node=uuid.uuid4(), stuck_reason="x")]
    assert stuck_count(quiet) == 0 and pick([registered, *quiet]) is not None
    # A key without the contract's shape is not judged by node state.
    odd = replace(registered, proxy_key="something-else")
    assert in_use(odd, {}) and pick([odd], nodes={}) is not None
    assert not in_use(replace(registered, proxy_key=None), LIVE_NODES)


def test_backoff_grows_and_honours_a_flood_wait() -> None:
    assert backoff(1, "telegram_unavailable", None) == timedelta(minutes=5)
    assert backoff(2, "no_reply", None) == timedelta(minutes=15)
    assert backoff(50, "no_reply", None) == timedelta(hours=6)
    assert backoff(1, "flood_wait", 3600) == timedelta(seconds=3605)
    assert backoff(0, "hourly_cap", None) == timedelta(minutes=10)
    assert backoff(0, "kill_switch", None) == timedelta(minutes=10)


# --- pacing (pure part) --------------------------------------------------------------------------------


def P(*sent: datetime, hold_until: datetime | None = None) -> pacing.Pace:
    return pacing.Pace(sent=tuple(sent), hold_until=hold_until)


def test_pacing_needs_five_seconds_between_messages_and_at_most_thirty_an_hour() -> None:
    assert pacing.decide(P(), NOW).ok
    decision = pacing.decide(P(NOW - timedelta(seconds=2)), NOW)
    assert not decision.ok and decision.stop is None and decision.wait_s == pytest.approx(3.0)
    assert pacing.decide(P(NOW - timedelta(seconds=5)), NOW).ok
    full = P(*[NOW - timedelta(minutes=59) + timedelta(seconds=10 * i) for i in range(pacing.HOURLY_CAP)])
    assert pacing.decide(full, NOW).stop == "hourly_cap" and pacing.remaining(full, NOW) == 0
    # A message an hour old no longer counts.
    assert pacing.decide(full, NOW + timedelta(minutes=1, seconds=1)).ok
    assert pacing.remaining(P(*full.sent[:10]), NOW) == pacing.HOURLY_CAP - 10
    # A stored time in the future (clock moved back) still counts, and blocks the gap.
    assert not pacing.decide(P(NOW + timedelta(seconds=30)), NOW).ok


def test_a_flood_wait_holds_every_message_until_it_has_passed() -> None:
    held = P(hold_until=NOW + timedelta(minutes=10))
    assert pacing.decide(held, NOW).stop == "hold" and pacing.remaining(held, NOW) == 0
    assert pacing.decide(held, NOW + timedelta(minutes=10)).ok
    assert pacing.remaining(held, NOW + timedelta(minutes=10)) == pacing.HOURLY_CAP


def test_the_stored_pacing_row_is_read_defensively() -> None:
    good = NOW.isoformat()
    value = {"sent": [good, "garbage", 5, "2026-11-04T12:00:00"], "hold_until": "nonsense"}
    assert pacing.Pace.from_json(value) == P(NOW)
    assert pacing.Pace.from_json(None) == P() and pacing.Pace.from_json({"sent": "x"}) == P()
    assert pacing.Pace.from_json(P(NOW, hold_until=NOW).to_json()) == P(NOW, hold_until=NOW)


# --- the provider's contract ---------------------------------------------------------------------------


def test_the_provider_contract_shape_is_checked_before_anything_is_sent() -> None:
    node = uuid.uuid4()
    good = Proxy(key=f"mtproto:{node}:p1", host="slot3.example.com", port=8443, secret_hex=SECRET)
    assert valid(good) and node_of(good.key) == node and user_of(good.key) == "p1"
    assert valid(replace(good, host="203.0.113.7"))
    for bad in (
        replace(good, secret_hex="ee" + SECRET),
        replace(good, secret_hex=SECRET.upper()),
        replace(good, secret_hex=SECRET[:-1]),
        replace(good, port=0),
        replace(good, port=70000),
        replace(good, port=True),
        replace(good, host="Slot3.Example.com"),
        replace(good, host="1.2.3.4:443"),
        replace(good, host=""),
        replace(good, key=""),
        replace(good, key="x" * 161),
    ):
        assert not valid(bad), bad
    assert node_of("reality:abc") is None and node_of(None) is None
    assert user_of("reality:abc") == "reality:abc"


# --- the driver's view of the chat ---------------------------------------------------------------------


def msg(
    mid: int, text: str, *, out: bool = False, edited: int | None = None, buttons: tuple = ()
) -> BotMessage:  # type: ignore[type-arg]
    return BotMessage(
        id=mid,
        outgoing=out,
        text=text,
        edit_date=NOW + timedelta(seconds=edited) if edited is not None else None,
        buttons=buttons,
    )


def test_only_the_bots_new_or_edited_messages_count_as_its_reply() -> None:
    before = [msg(10, "old"), msg(11, "/myproxies", out=True)]
    menu = (BotButton("Set promotion", b"p"),)
    after = [
        msg(10, "old"),  # unchanged
        msg(11, "/myproxies", out=True),
        msg(12, "/newproxy", out=True),  # ours
        msg(13, "Registering a new proxy server."),  # new from the bot
    ]
    assert [m.id for m in changes(before, after)] == [13]
    edited = [msg(10, "old", edited=5, buttons=(menu,)), msg(11, "/myproxies", out=True)]
    assert [m.id for m in changes(before, edited)] == [10]  # the bot edited its message in place
    assert changes(before, before) == []
    # A message older than everything seen (scrolled into view) is not a reply.
    assert changes([msg(20, "x")], [msg(15, "older"), msg(20, "x")]) == []
