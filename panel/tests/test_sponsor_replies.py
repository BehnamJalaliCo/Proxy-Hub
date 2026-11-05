"""@MTProxybot's replies as DATA (decision D10: nothing imitates the bot; no fake chat partner).

The published replies are kept word for word (``REAL_*``): from the transcript in
TelegramMessenger/MTProxy issue #112 (the bot's three registration replies, its success reply with
the tag and the links that echo the secret, the /myproxies header) and telemt docs/FAQ.en.md (the
"Set promotion" button). The prompt for the channel link and the confirmation after it have never
been published; the ``ASSUMED_*`` texts below are plausible wordings that check the matching by
meaning, and the first real run records the true ones (docs/11 F11).

Each test feeds a reply to the pure state machine (proxyhub/sponsor/conversation.py) and checks the
next action or result. Unknown replies must stop the flow.
"""

from __future__ import annotations

import base64
import secrets

import pytest

from proxyhub.mtproxy.userclient import BotButton, BotMessage
from proxyhub.sponsor import replies
from proxyhub.sponsor.channels import ChannelError, channel_link, parse_public_channel
from proxyhub.sponsor.conversation import (
    MESSAGES_NEEDED,
    Observation,
    Press,
    PromoteJob,
    PromotionSet,
    Registered,
    RegisterJob,
    Send,
    Step,
    Stuck,
    first_action,
    next_action,
)

# --- the published replies (word for word) --------------------------------------------------------------

REAL_ASK_ADDRESS = "Registering a new proxy server. Please send me its address in the format host:port."
REAL_ASK_SECRET = "Now please specify its secret in hex format."
REAL_TAG = "ecfe4482355b04af36e816044653e999"  # the issue's example tag
REAL_SECRET = "123456789012345678901234567890ab"  # the issue's example secret (not a real proxy's)
REAL_SUCCESS = [
    "Success!",
    "Your proxy has been successfully registered. "
    "You can now pass this proxy tag to the software you are using:",
    f"{REAL_TAG}.",
    "Here is a link to your proxy server:",
    f"https://t.me/proxy?server=1.2.3.4&port=443&secret={REAL_SECRET}",
    "And here is a direct link for those who have the Telegram app installed:",
    f"tg://proxy?server=1.2.3.4&port=443&secret={REAL_SECRET}",
]
REAL_LIST_HEADER = "Here is the list of all proxies you created:"
REAL_SET_PROMOTION = "Set promotion"

# --- wordings never published (assumed) ----------------------------------------------------------------

ASSUMED_ASK_CHANNEL = [
    "Please send me a public link to the channel you want to promote. Private channels are not supported.",
    "OK. Send me the channel's @username or its t.me link.",
    "Enter the link to your channel",
]
ASSUMED_PROMOTION_DONE = [
    "Success! The promoted channel has been set. It may take up to an hour to apply.",
    "Done! Promotion updated.",
    "The channel will be shown to users of this proxy within an hour.",
]

JOB = RegisterJob(host="1.2.3.4", port=443, secret_hex=REAL_SECRET)
PROMO = PromoteJob(host="1.2.3.4", port=443, channel="ipoxxo")


def bot(text: str, *, mid: int = 100, buttons: tuple[tuple[BotButton, ...], ...] = ()) -> BotMessage:
    return BotMessage(id=mid, outgoing=False, text=text, edit_date=None, buttons=buttons)


def seen(*texts: str, answer: str | None = None) -> Observation:
    return Observation(messages=tuple(bot(t, mid=100 + i) for i, t in enumerate(texts)), answer=answer)


def row(*labels: str) -> tuple[BotButton, ...]:
    return tuple(BotButton(label, f"cb:{label}".encode()) for label in labels)


# --- registration --------------------------------------------------------------------------------------


def test_registration_follows_the_published_replies_to_the_tag() -> None:
    first = first_action(JOB)
    assert first == Send(text="/newproxy", shown="/newproxy", then=Step.SENT_NEWPROXY)
    step2 = next_action(JOB, Step.SENT_NEWPROXY, seen(REAL_ASK_ADDRESS))
    assert step2 == Send(text="1.2.3.4:443", shown="1.2.3.4:443", then=Step.SENT_ADDRESS)
    step3 = next_action(JOB, Step.SENT_ADDRESS, seen(REAL_ASK_SECRET))
    assert isinstance(step3, Send) and step3.text == REAL_SECRET and step3.sensitive
    assert step3.shown == "[secret]" and REAL_SECRET not in repr(step3)
    assert next_action(JOB, Step.SENT_SECRET, seen(*REAL_SUCCESS)) == Registered(tag=REAL_TAG)
    assert MESSAGES_NEEDED[RegisterJob] == 3


@pytest.mark.parametrize(
    "reply",
    [
        seen("\n".join(REAL_SUCCESS)),  # one message
        seen(*REAL_SUCCESS),  # one message per line
        seen("Success!" + REAL_SUCCESS[1], REAL_TAG, *REAL_SUCCESS[3:]),  # "Success!Your proxy ..." (a blog)
        seen("\n".join(REAL_SUCCESS[:3])),  # the links in a later message that never came
    ],
)
def test_the_tag_is_read_from_every_published_shape_of_the_success_reply(reply: Observation) -> None:
    assert next_action(JOB, Step.SENT_SECRET, reply) == Registered(tag=REAL_TAG)


def test_the_tag_is_never_the_secret_and_never_taken_from_a_link() -> None:
    # Only the links carry a 32-hex value besides the secret: no tag.
    text = "\n".join([REAL_SUCCESS[1], f"https://t.me/proxy?server=1.2.3.4&port=443&secret={'ab' * 16}"])
    assert replies.registered_tag(text, REAL_SECRET) is None
    # The secret printed outside a link is not taken for the tag.
    assert replies.registered_tag(f"{REAL_SUCCESS[1]}\n{REAL_SECRET}", REAL_SECRET) is None
    # Two different tags: ambiguous.
    assert replies.registered_tag(f"{REAL_SUCCESS[1]}\n{REAL_TAG}\n{'cd' * 16}", REAL_SECRET) is None
    # An all-zero tag has no effect in Telemt (docs/11 E1).
    assert replies.registered_tag(f"{REAL_SUCCESS[1]}\n{'0' * 32}", REAL_SECRET) is None
    # Upper-case in the reply: stored lower-case (the desired state's format).
    assert replies.registered_tag(f"{REAL_SUCCESS[1]}\n{REAL_TAG.upper()}", REAL_SECRET) == REAL_TAG


@pytest.mark.parametrize(
    ("step", "reply", "reason", "hint"),
    [
        (Step.SENT_NEWPROXY, "Unrecognized command. Say what?", "unexpected_reply", None),
        (Step.SENT_NEWPROXY, REAL_ASK_SECRET, "unexpected_reply", None),  # a step was skipped: no guessing
        (
            Step.SENT_NEWPROXY,
            "Sorry, you have too many proxies. Try again later.",
            "unexpected_reply",
            "bot_limit",
        ),
        (Step.SENT_ADDRESS, "Invalid address. " + REAL_ASK_ADDRESS, "address_refused", "invalid_input"),
        (Step.SENT_ADDRESS, "This proxy is already registered.", "address_refused", "already_registered"),
        (Step.SENT_SECRET, "Invalid secret. " + REAL_ASK_SECRET, "no_tag_in_reply", "invalid_input"),
        (Step.SENT_SECRET, "Success!", "no_tag_in_reply", None),  # the tag never came
        (Step.SENT_SECRET, REAL_TAG, "no_tag_in_reply", None),  # a bare hex value is not the success reply
    ],
)
def test_any_other_registration_reply_stops_the_flow(
    step: Step, reply: str, reason: str, hint: str | None
) -> None:
    assert next_action(JOB, step, seen(reply)) == Stuck(step=step, reason=reason, hint=hint)


def test_an_empty_or_misplaced_observation_stops_too() -> None:
    assert isinstance(next_action(JOB, Step.SENT_NEWPROXY, Observation(messages=())), Stuck)
    assert next_action(JOB, Step.SENT_MYPROXIES, seen(REAL_ASK_ADDRESS)) == Stuck(
        step=Step.SENT_MYPROXIES, reason="wrong_step"
    )
    assert next_action(PROMO, Step.SENT_SECRET, seen(*REAL_SUCCESS)) == Stuck(
        step=Step.SENT_SECRET, reason="wrong_step"
    )


# --- promotion -----------------------------------------------------------------------------------------


def _list(*labels: str, header: str = REAL_LIST_HEADER) -> Observation:
    return Observation(messages=(bot(header, mid=200, buttons=(row(*labels),)),))


def test_promotion_follows_the_published_steps_to_the_channel_link() -> None:
    assert first_action(PROMO) == Send(text="/myproxies", shown="/myproxies", then=Step.SENT_MYPROXIES)
    pick = next_action(PROMO, Step.SENT_MYPROXIES, _list("5.6.7.8:443", "1.2.3.4:443", "1.2.3.4:4433"))
    assert pick == Press(message_id=200, data=b"cb:1.2.3.4:443", label="1.2.3.4:443", then=Step.PRESSED_PROXY)
    menu = Observation(
        messages=(
            bot(
                "1.2.3.4:443",
                mid=200,
                buttons=(row(REAL_SET_PROMOTION, "Delete proxy"), row("Stats", "« Back")),
            ),
        )
    )
    press = next_action(PROMO, Step.PRESSED_PROXY, menu)
    assert press == Press(
        message_id=200, data=b"cb:Set promotion", label=REAL_SET_PROMOTION, then=Step.PRESSED_PROMOTION
    )
    for prompt in ASSUMED_ASK_CHANNEL:
        link = next_action(PROMO, Step.PRESSED_PROMOTION, seen(prompt))
        assert link == Send(text="https://t.me/ipoxxo", shown="https://t.me/ipoxxo", then=Step.SENT_CHANNEL)
    # The prompt may come only as the short answer to the button press.
    assert isinstance(next_action(PROMO, Step.PRESSED_PROMOTION, seen(answer=ASSUMED_ASK_CHANNEL[2])), Send)
    for done in ASSUMED_PROMOTION_DONE:
        assert next_action(PROMO, Step.SENT_CHANNEL, seen(done)) == PromotionSet(channel="ipoxxo")
    assert MESSAGES_NEEDED[PromoteJob] == 4


def test_labels_with_emoji_or_a_domain_name_still_match_exactly() -> None:
    job = PromoteJob(host="slot3.example.com", port=8443, channel="ipoxxo")
    pick = next_action(
        job, Step.SENT_MYPROXIES, _list("🟢 slot3.example.com:8443", "slot33.example.com:8443")
    )
    assert isinstance(pick, Press) and pick.data == "cb:🟢 slot3.example.com:8443".encode()
    # A delete button with the same address next to it is never the one pressed.
    pick = next_action(
        job, Step.SENT_MYPROXIES, _list("🗑 Delete slot3.example.com:8443", "slot3.example.com:8443")
    )
    assert isinstance(pick, Press) and pick.data == b"cb:slot3.example.com:8443"
    menu = Observation(messages=(bot("slot3.example.com:8443", buttons=(row("⭐️ Set Promotion"),)),))
    assert isinstance(next_action(job, Step.PRESSED_PROXY, menu), Press)


@pytest.mark.parametrize(
    ("step", "observation", "reason"),
    [
        (Step.SENT_MYPROXIES, _list("1.2.3.4:443", header="Choose a proxy"), "unexpected_reply"),
        (Step.SENT_MYPROXIES, _list("5.6.7.8:443", "1.2.3.4:4433"), "proxy_not_in_list"),
        (Step.SENT_MYPROXIES, _list(), "proxy_not_in_list"),
        (
            Step.SENT_MYPROXIES,
            Observation(
                messages=(
                    bot(
                        REAL_LIST_HEADER,
                        buttons=((BotButton("1.2.3.4:443", b"a"),), (BotButton("1.2.3.4:443", b"b"),)),
                    ),
                )
            ),
            "proxy_listed_twice",
        ),
        (  # a link button with our address cannot be pressed
            Step.SENT_MYPROXIES,
            Observation(messages=(bot(REAL_LIST_HEADER, buttons=((BotButton("1.2.3.4:443", None),),)),)),
            "proxy_not_in_list",
        ),
        (
            Step.PRESSED_PROXY,
            Observation(messages=(bot("1.2.3.4:443", buttons=(row("Remove promotion", "Delete proxy"),)),)),
            "no_set_promotion_button",
        ),
        (
            Step.PRESSED_PROXY,
            Observation(messages=(bot("5.6.7.8:443", buttons=(row(REAL_SET_PROMOTION),)),)),
            "menu_of_another_proxy",
        ),
        (
            Step.PRESSED_PROXY,
            Observation(
                messages=(bot("1.2.3.4:443", buttons=(row(REAL_SET_PROMOTION), row("Edit promotion"))),)
            ),
            "several_promotion_buttons",
        ),
        (Step.PRESSED_PROMOTION, seen("Sorry, something went wrong."), "no_channel_prompt"),
        (Step.PRESSED_PROMOTION, seen("Stats for the last 24 hours: 12 users"), "no_channel_prompt"),
        (Step.SENT_CHANNEL, seen("Sorry, private channels cannot be promoted."), "channel_refused"),
        (Step.SENT_CHANNEL, seen("You must be an administrator of this channel."), "channel_refused"),
        (Step.SENT_CHANNEL, seen("Channel not found."), "channel_refused"),
        (Step.SENT_CHANNEL, seen("Hmm."), "channel_refused"),
        # A success word next to a refusal is a refusal (a paid campaign must never be reported
        # as delivered when it was not) ...
        (Step.SENT_CHANNEL, seen("Sorry, channel not found. Done."), "channel_refused"),
        (Step.SENT_CHANNEL, seen("Success! But this channel is private."), "channel_refused"),
        (Step.SENT_CHANNEL, seen("Done.", answer="Error: try again later"), "channel_refused"),
        # ... and so is anything that took something away.
        (Step.SENT_CHANNEL, seen("Done! Proxy deleted"), "channel_refused"),
        (Step.SENT_CHANNEL, seen("Saved. The proxy was removed."), "channel_refused"),
        (Step.SENT_CHANNEL, seen("Success! Promotion disabled."), "channel_refused"),
        # A /myproxies button is pressed only when its label names our address and nothing but hex.
        (Step.SENT_MYPROXIES, _list("🗑 Delete 1.2.3.4:443"), "proxy_not_in_list"),
        (Step.SENT_MYPROXIES, _list("1.2.3.4:443 remove"), "proxy_not_in_list"),
        (Step.SENT_MYPROXIES, _list("11.2.3.4 1a2b3c"), "proxy_not_in_list"),
        (Step.SENT_MYPROXIES, _list("1.2.3.4 1a2b 3c4d 5e6f"), "proxy_not_in_list"),
        (Step.SENT_MYPROXIES, _list("1.2.3.4 1a2b", "1.2.3.4:443"), "proxy_listed_twice"),
    ],
)
def test_any_other_promotion_reply_stops_the_flow(step: Step, observation: Observation, reason: str) -> None:
    result = next_action(PROMO, step, observation)
    assert isinstance(result, Stuck) and result.step == step and result.reason == reason


def test_the_real_myproxies_label_shape_is_matched() -> None:
    # P13 on the panel server: @MTProxybot labels each /myproxies button "<host> <hex>".
    job = PromoteJob(host="t7aae9f0c.ipoxxo.online", port=8443, channel="ipoxxo")
    for label in (
        "t7aae9f0c.ipoxxo.online 4f1c9a0b",
        "t7aae9f0c.ipoxxo.online:8443",
        "🌐 t7aae9f0c.ipoxxo.online 4f1c",
    ):
        assert replies.is_address_label(label, job.host, job.port), label
    for label in (
        "x.t7aae9f0c.ipoxxo.online 4f1c",  # another host that ends like ours
        "t7aae9f0c.ipoxxo.online.evil.com 4f1c",
        "Delete t7aae9f0c.ipoxxo.online",
        "t7aae9f0c.ipoxxo.online 4f1c remove",
        "91.107.189.145 4f1c",  # our old registration of another address
    ):
        assert not replies.is_address_label(label, job.host, job.port), label
    pick = next_action(
        job, Step.SENT_MYPROXIES, _list("91.107.189.145 9e8d7c6b", "t7aae9f0c.ipoxxo.online 4f1c9a0b")
    )
    assert isinstance(pick, Press) and pick.label == "t7aae9f0c.ipoxxo.online 4f1c9a0b"


def test_a_refusal_is_explained_to_the_owner() -> None:
    assert replies.refusal_hint("You must be an administrator of this channel.") == "admin_required"  # F12
    assert replies.refusal_hint("Sorry, private channels cannot be promoted.") == "channel_private"
    assert replies.refusal_hint("This proxy is already registered.") == "already_registered"
    assert replies.refusal_hint("Hmm.") is None


def test_the_delete_button_is_never_a_promotion_button() -> None:
    for label in ("Delete proxy", "Remove promotion", "Delete promotion", "Reset", "Revoke"):
        assert replies.normalize_label(label) not in replies.PROMOTION_LABELS
    assert replies.normalize_label("  ⭐️ Set   PROMOTION ") == "set promotion"


# --- masking -------------------------------------------------------------------------------------------


def test_the_success_reply_is_stored_without_the_secret() -> None:
    secret = secrets.token_hex(16)
    raw = "\n".join(s.replace(REAL_SECRET, secret) for s in REAL_SUCCESS)
    masked = replies.mask(raw, secrets=[secret], tags=[REAL_TAG])
    assert secret not in masked.lower()
    assert "secret=[secret]" in masked and "ecfe…e999" in masked and "Success!" in masked
    unknown = replies.mask(raw)  # even without knowing the secret: secret= values and hex runs go
    assert secret not in unknown.lower() and REAL_TAG not in unknown
    # A link with a secret in another encoding (base64url Fake-TLS) that nobody told us about.
    other = base64.urlsafe_b64encode(bytes.fromhex("ee" + secrets.token_hex(16)) + b"example.com").decode()
    link = f"tg://proxy?server=1.2.3.4&port=443&secret={other.rstrip('=')}&x=1"
    assert replies.mask(link) == "tg://proxy?server=1.2.3.4&port=443&secret=[secret]&x=1"


def test_every_encoding_of_the_secret_is_masked() -> None:
    secret = secrets.token_hex(16)
    raw = bytes.fromhex(secret)
    hex_forms = [secret, secret.upper(), "ee" + secret + "7777772e676f6f676c652e636f6d"]  # + Fake-TLS
    b64_forms = [base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("=")]
    for form in hex_forms:  # any run of 32+ hex characters goes, known or not
        assert secret not in replies.mask(f"your secret: {form} (keep it)").lower()
    for form in hex_forms + b64_forms:  # the known secret goes in every encoding
        masked = replies.mask(f"your secret: {form} (keep it)", secrets=[secret])
        assert form not in masked and secret not in masked.lower(), form
        assert masked.startswith("your secret: ") and masked.endswith(" (keep it)")
    long = replies.mask("x" * 5000)
    assert len(long) == replies.MAX_STORED_TEXT and long.endswith("…")
    assert replies.short_tag(None) is None and replies.short_tag("ab") == "…"


# --- public channel links --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/IpoXXo",
        "http://t.me/ipoxxo/",
        "t.me/ipoxxo",
        "@ipoxxo",
        "ipoxxo",
        " https://telegram.me/ipoxxo ",
    ],
)
def test_public_channel_links_become_the_username(raw: str) -> None:
    assert parse_public_channel(raw) == "ipoxxo"
    assert channel_link("ipoxxo") == "https://t.me/ipoxxo"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("https://t.me/+AbCdEfGh123", "channel_private"),
        ("https://t.me/joinchat/AAAAAEb0", "channel_private"),
        ("+AbCdEfGh123", "channel_private"),
        ("https://t.me/proxy?server=1.2.3.4&port=443&secret=ab", "channel_format"),
        ("https://example.com/ipoxxo", "channel_format"),
        ("https://t.me/ipoxxo/123", "channel_format"),
        ("ipo", "channel_format"),
        ("9ipoxxo", "channel_format"),
        ("ipoxxo_", "channel_format"),
        ("ipo xxo", "channel_format"),
        ("", "channel_format"),
        ("a" * 33, "channel_format"),
    ],
)
def test_private_or_malformed_links_are_refused(raw: str, code: str) -> None:
    with pytest.raises(ChannelError) as caught:
        parse_public_channel(raw)
    assert caught.value.code == code
