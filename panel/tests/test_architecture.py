"""Rules from CLAUDE.md enforced on the source tree itself.

Each rule has a negative control (a deliberately bad snippet) so we know the check works.
"""

from __future__ import annotations

import re
from pathlib import Path

PANEL = Path(__file__).resolve().parents[1] / "proxyhub"
REPO = PANEL.parents[1]

# Rule 6: posts are never deleted. No Telegram delete call may exist anywhere in the code. Reading
# the admin flag to warn the owner is allowed: the Bot API's can_delete_messages, and (D38) the
# operator account's own ``admin_rights.delete_messages`` as an attribute, never called. The bot
# client also refuses every method outside its allowlist (proxyhub/telegram/client.py), and the
# operator client every request outside its own (proxyhub/mtproxy/userclient.py).
TG_DELETE = re.compile(
    r"(?<!can_)(?<!admin_rights\.)delete_?messages?\b|admin_rights\.delete_?messages?\s*\(|deleteMessages?\b"
    r"|\bDelete\w*Request\b|delete_?history\b",
    re.IGNORECASE,
)
# Rule 5: only proxyhub/hetzner/mutations.py may create or delete servers.
HCLOUD_MUTATION = re.compile(r"\.servers\.(create|delete|rebuild)\(|\bdelete_server\(|\bcreate_server\(")
# Rule 7: never call APIs that return user IPs (Xray online-IP stats, telemt IP lists).
IP_APIS = re.compile(
    r"GetStatsOnlineIpList|GetUsersStats|statsUserOnline\"?\s*[:=]\s*true|"
    r"/v1/stats/users/active-ips|/v1/users\b|active_ips|unique_ips_list",
    re.IGNORECASE,
)
# D35: a whole Cloudflare zone is deleted in exactly one place, after every condition was re-checked
# inside the deciding transaction (proxyhub/cloudflare/deletion.py); the client method is the only
# code that builds the request.
CF_ZONE_DELETE_CALL = re.compile(r"\.delete_zone\(")
CF_ZONE_DELETE_DEF = re.compile(r"\bdef delete_zone\(")
CF_ZONE_DELETE_REQUEST = re.compile(r"[\"']DELETE[\"'],\s*f?[\"']/zones/\{[a-z_]+\}[\"']\s*\)")
CF_ZONE_DELETE_CALLER = PANEL / "cloudflare" / "deletion.py"
CF_CLIENT = PANEL / "cloudflare" / "client.py"
# D37 / docs/11 section 2.1: Telethon (a full user account client) is imported in exactly one
# module, whose request allowlist keeps the operator account to @MTProxybot. Nothing anywhere in the
# panel may even name the calls that list chats or contacts or read a chat's messages.
TELETHON_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+telethon\b|import_module\(\s*[\"']telethon|__import__\(\s*[\"']telethon",
    re.MULTILINE,
)
TELETHON_MODULE = PANEL / "mtproxy" / "userclient.py"
USER_ACCOUNT_READS = re.compile(
    r"get_dialogs|iter_dialogs|GetDialogsRequest|get_contacts|GetContactsRequest|iter_messages|get_messages"
)
# proxyhub/sponsor talks to @MTProxybot, and only through userclient.BotChat: the raw requests that
# write to or read a chat are named in userclient.py alone (where the allowlist pins their peer to
# the bot), and no other request of Telegram's messages namespace is named anywhere.
BOT_CHAT_REQUEST_NAMES = re.compile(
    r"SendMessageRequest|GetHistoryRequest|GetBotCallbackAnswerRequest|EditMessageRequest"
)
MESSAGES_NAMESPACE = re.compile(r"functions\.messages\.(\w+)")
CHANNELS_NAMESPACE = re.compile(r"functions\.channels\.(\w+)")
BOT_CHAT_ONLY = {"SendMessageRequest", "GetHistoryRequest", "GetBotCallbackAnswerRequest"}
# D38: the one messages.* read outside the bot chat. Channel statistics read the view and forward
# counts of OUR channels' posts, never incrementing a view (proxyhub/mtproxy/channel_stats.py).
CHANNEL_STATS_MESSAGES = {"GetMessagesViewsRequest"}
# D38, the operator poster (proxyhub/posts/operator_outbox.py): a post (messages.sendMessage, the
# same request class as the bot chat's, pinned to the scope's post channels) and the EDIT of a post
# in OUR channels, and the read of the owner's one emoji pack. Nothing else of messages.*.
CHANNEL_POST_MESSAGES = {"EditMessageRequest", "GetStickerSetRequest"}
# channels.*: the statistics' full channel, and (D38) the account's own membership (its rights).
CHANNELS_REQUESTS = {"GetFullChannelRequest", "GetParticipantRequest"}
# Every request class the operator client can EVER let through (any scope, any bot chat), by TL
# name. Nothing that deletes, forwards, uploads, joins, leaves, reports, invites or changes rights.
OPERATOR_PASSABLE = {
    "auth.SendCodeRequest",
    "auth.SignInRequest",
    "auth.CheckPasswordRequest",
    "auth.LogOutRequest",
    "account.GetPasswordRequest",
    "users.GetUsersRequest",
    "contacts.ResolveUsernameRequest",
    "help.GetConfigRequest",
    "updates.GetStateRequest",
    "channels.GetFullChannelRequest",
    "channels.GetParticipantRequest",
    "stats.GetBroadcastStatsRequest",
    "stats.LoadAsyncGraphRequest",
    "messages.GetMessagesViewsRequest",
    "messages.GetStickerSetRequest",
    "messages.SendMessageRequest",
    "messages.EditMessageRequest",
    "messages.GetHistoryRequest",
    "messages.GetBotCallbackAnswerRequest",
}
FORBIDDEN_REQUEST_WORDS = re.compile(
    r"Delete|Forward|Upload|Media|Join|Leave|Report|Invite|Edit(?:Admin|Banned|Creator|Title|Photo)|"
    r"Block|Import|Export|Install|Save|Read|Clear|Pin|Search|Dialog|Contact",
)
# Telethon's high-level helpers that write to, or read, any chat (the sponsor code must not reach
# around the allowlisted BotChat).
USER_ACCOUNT_WRITES = re.compile(
    r"\.send_message\(|\.send_file\(|\.forward_messages\(|\.edit_message\(|\.click\(|\.conversation\("
    r"|\.inline_query\(|\.get_entity\(|\.get_input_entity\("
)
# mutations.py does the hcloud calls; the node executor is the single caller, holding gate permits
# (test nodes only; tests/test_live_units.py::test_only_the_executor_reaches_server_mutations_and_permits).
ALLOWED_MUTATION_FILES = {PANEL / "hetzner" / "mutations.py", PANEL / "nodes" / "executor.py"}


def _sources() -> list[Path]:
    return [p for p in PANEL.rglob("*.py") if "migrations" not in p.parts]


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_negative_controls_match() -> None:
    assert TG_DELETE.search("await bot.delete_message(chat, 1)")
    assert TG_DELETE.search('requests.post(url + "/deleteMessage")')
    assert TG_DELETE.search("client.delete_messages(chat, [1, 2])")
    assert not TG_DELETE.search('member.get("can_delete_messages")')
    # D38: the operator account's own delete flag may be READ (to warn the owner), never called,
    # and no other object's delete_messages may even be named.
    assert not TG_DELETE.search("can_delete=bool(admin_rights.delete_messages),")
    assert TG_DELETE.search("await admin_rights.delete_messages(chat, [1])")
    assert TG_DELETE.search("await admin_rights.delete_messages (chat, [1])")
    assert TG_DELETE.search("rights.delete_messages")
    assert TG_DELETE.search('getattr(rights, "delete_messages")')
    # MTProto's delete requests by their TL names (no word boundary inside "DeleteMessagesRequest").
    assert TG_DELETE.search("functions.channels.DeleteMessagesRequest(c, [1])")
    assert TG_DELETE.search("functions.messages.DeleteMessagesRequest(id=[1], revoke=True)")
    assert TG_DELETE.search("functions.messages.DeleteHistoryRequest(peer, max_id=0)")
    assert TG_DELETE.search("functions.channels.DeleteHistoryRequest(channel, max_id=0)")
    assert TG_DELETE.search("await client.delete_history(peer)")
    assert FORBIDDEN_REQUEST_WORDS.search("messages.DeleteMessagesRequest")
    assert FORBIDDEN_REQUEST_WORDS.search("channels.DeleteHistoryRequest")
    assert FORBIDDEN_REQUEST_WORDS.search("messages.ForwardMessagesRequest")
    assert FORBIDDEN_REQUEST_WORDS.search("channels.EditAdminRequest")
    assert not FORBIDDEN_REQUEST_WORDS.search("messages.EditMessageRequest")
    assert CHANNELS_NAMESPACE.findall("functions.channels.LeaveChannelRequest(c)") == ["LeaveChannelRequest"]
    assert BOT_CHAT_REQUEST_NAMES.search("functions.messages.EditMessageRequest(peer=p, id=1, message=m)")
    assert HCLOUD_MUTATION.search("client.servers.delete(server)")
    assert IP_APIS.search('httpx.get("http://127.0.0.1:9091/v1/users")')
    assert IP_APIS.search('"statsUserOnline": true')
    assert CF_ZONE_DELETE_CALL.search("await cf.delete_zone(zone_id)")
    assert not CF_ZONE_DELETE_CALL.search("async def delete_zone(self, zone_id: str) -> None:")
    assert CF_ZONE_DELETE_DEF.search("    async def delete_zone(self, zone_id: str) -> None:")
    assert CF_ZONE_DELETE_REQUEST.search('await self._request("DELETE", f"/zones/{zone_id}")')
    assert not CF_ZONE_DELETE_REQUEST.search('self._request("DELETE", f"/zones/{zone_id}/dns_records/{rid}")')
    assert TELETHON_IMPORT.search("from telethon import TelegramClient")
    assert TELETHON_IMPORT.search("    import telethon.sessions")
    assert TELETHON_IMPORT.search('mod = importlib.import_module("telethon.tl.functions")')
    assert not TELETHON_IMPORT.search("from proxyhub.mtproxy.userclient import operator_session")
    assert not TELETHON_IMPORT.search('"""Telethon is imported only in userclient.py."""')
    for bad in (
        "await client.get_dialogs()",
        "async for d in client.iter_dialogs():",
        "functions.messages.GetDialogsRequest(",
        "await client.get_contacts()",
        "functions.contacts.GetContactsRequest(hash=0)",
        "async for m in client.iter_messages(peer):",
        "await client.get_messages(peer, limit=5)",
    ):
        assert USER_ACCOUNT_READS.search(bad), bad
    assert BOT_CHAT_REQUEST_NAMES.search("functions.messages.SendMessageRequest(peer=p, message=m)")
    assert BOT_CHAT_REQUEST_NAMES.search("GetBotCallbackAnswerRequest(peer, msg_id=1, data=b'x')")
    assert MESSAGES_NAMESPACE.findall("functions.messages.ReadHistoryRequest(peer)") == ["ReadHistoryRequest"]
    for bad in (
        "await client.send_message('durov', 'hi')",
        "await msg.click(0)",
        "async with client.conversation(bot) as conv:",
        "await client.get_entity('someone')",
        "await client.get_input_entity(peer)",
    ):
        assert USER_ACCOUNT_WRITES.search(bad), bad
    assert not USER_ACCOUNT_WRITES.search("await chat.send('/newproxy')")
    assert not USER_ACCOUNT_WRITES.search("await chat.press(message_id, data)")


def test_no_telegram_delete_anywhere() -> None:
    hits = [p for p in _sources() if TG_DELETE.search(_strip_comments(p.read_text(encoding="utf-8")))]
    assert not hits, f"rule 6 violated in {hits}"


ACME_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:acme|josepy)\b", re.MULTILINE)
ACME_MODULE = PANEL / "acme" / "protocol.py"


def test_the_acme_library_is_imported_only_by_the_protocol_module() -> None:
    """certbot's ACME library logs request bodies at DEBUG; one module holds its logger at INFO and
    turns every failure into a short code, so nothing else may talk to it directly."""
    assert ACME_IMPORT.search("from acme import messages") and ACME_IMPORT.search("import josepy as jose")
    assert not ACME_IMPORT.search("from proxyhub.acme import store")
    importers = {p for p in _sources() if ACME_IMPORT.search(p.read_text(encoding="utf-8"))}
    assert importers == {ACME_MODULE}, importers


def test_server_mutations_only_in_mutations_module() -> None:
    hits = [
        p
        for p in _sources()
        if p not in ALLOWED_MUTATION_FILES
        and HCLOUD_MUTATION.search(_strip_comments(p.read_text(encoding="utf-8")))
    ]
    assert not hits, f"rule 5 violated: server create/delete outside mutations.py in {hits}"


def test_no_user_ip_apis() -> None:
    roots = [PANEL, REPO / "agent", REPO / "node-image"]
    files = [
        p for r in roots if r.exists() for p in r.rglob("*") if p.suffix in {".py", ".go", ".toml", ".json"}
    ]
    hits = [p for p in files if IP_APIS.search(_strip_comments(p.read_text(encoding="utf-8")))]
    assert not hits, f"rule 7 violated in {hits}"


def _count(pattern: re.Pattern[str]) -> dict[Path, int]:
    found = {p: len(pattern.findall(_strip_comments(p.read_text(encoding="utf-8")))) for p in _sources()}
    return {p: n for p, n in found.items() if n}


def test_cloudflare_zone_delete_has_exactly_one_call_site() -> None:
    assert _count(CF_ZONE_DELETE_CALL) == {CF_ZONE_DELETE_CALLER: 1}, "D35: one caller of delete_zone"
    assert _count(CF_ZONE_DELETE_DEF) == {CF_CLIENT: 1}
    assert _count(CF_ZONE_DELETE_REQUEST) == {CF_CLIENT: 1}, "DELETE /zones/{id} is built only in the client"


def test_telethon_is_imported_only_by_the_operator_client() -> None:
    importers = {p for p in _sources() if TELETHON_IMPORT.search(p.read_text(encoding="utf-8"))}
    assert importers == {TELETHON_MODULE}, f"Telethon imported outside userclient.py: {importers}"


def test_no_call_that_reads_chats_or_contacts_is_named_anywhere() -> None:
    # Comments and docstrings included: the names must not appear in the panel at all.
    hits = {
        p: USER_ACCOUNT_READS.findall(p.read_text(encoding="utf-8"))
        for p in PANEL.rglob("*.py")
        if USER_ACCOUNT_READS.search(p.read_text(encoding="utf-8"))
    }
    assert not hits, f"docs/11 section 2.1 lock 1 violated: {hits}"


def test_chat_requests_are_named_only_by_the_operator_client() -> None:
    # Comments and docstrings included: only userclient.py builds a request that writes to or reads
    # a chat, and its allowlist pins every one of them to @MTProxybot.
    named = {p for p in PANEL.rglob("*.py") if BOT_CHAT_REQUEST_NAMES.search(p.read_text(encoding="utf-8"))}
    assert named == {TELETHON_MODULE}, f"chat requests named outside userclient.py: {named}"
    used: set[str] = set()
    channels: set[str] = set()
    for p in _sources():
        text = _strip_comments(p.read_text(encoding="utf-8"))
        used |= set(MESSAGES_NAMESPACE.findall(text))
        channels |= set(CHANNELS_NAMESPACE.findall(text))
    assert used == BOT_CHAT_ONLY | CHANNEL_STATS_MESSAGES | CHANNEL_POST_MESSAGES, (
        f"messages.* requests in the panel: {sorted(used)}"
    )
    assert channels == CHANNELS_REQUESTS, f"channels.* requests in the panel: {sorted(channels)}"


def _operator_account_code() -> list[Path]:
    """Every module that can hold the operator account's client: the mtproxy and sponsor packages
    and anything that imports from them (the panel's own Bot API client has its own send_message)."""
    found = []
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        if {"mtproxy", "sponsor"} & set(p.relative_to(PANEL).parts[:1]) or re.search(
            r"from proxyhub\.(mtproxy|sponsor)\b|import proxyhub\.(mtproxy|sponsor)\b", text
        ):
            found.append(p)
    return found


def test_no_high_level_chat_helper_is_called_by_operator_account_code() -> None:
    files = _operator_account_code()
    assert TELETHON_MODULE in files and PANEL / "reconcilers" / "mtproxy_account.py" in files
    hits = {
        p: USER_ACCOUNT_WRITES.findall(_strip_comments(p.read_text(encoding="utf-8")))
        for p in files
        if USER_ACCOUNT_WRITES.search(_strip_comments(p.read_text(encoding="utf-8")))
    }
    assert not hits, f"docs/11 section 2.1 lock 1: use userclient.BotChat only: {hits}"


def test_the_bot_chat_allowlist_is_exactly_three_requests_pinned_to_the_bot() -> None:
    from telethon import functions, types

    from proxyhub.mtproxy.userclient import (
        ALLOWED_REQUESTS,
        BOT_CHAT_REQUESTS,
        MAX_HISTORY_LIMIT,
        RequestNotAllowed,
        check_request,
    )

    assert {k.__name__ for k in BOT_CHAT_REQUESTS} == BOT_CHAT_ONLY
    assert not BOT_CHAT_REQUESTS & ALLOWED_REQUESTS
    in_messages = {k.__name__ for k in ALLOWED_REQUESTS if k.__module__.endswith(".messages")}
    # read-only: our channels' view counts, and the owner's one emoji pack (D38)
    assert in_messages == CHANNEL_STATS_MESSAGES | {"GetStickerSetRequest"}
    other = types.InputPeerChannel(channel_id=1_234_567, access_hash=1)
    for views in (
        functions.messages.GetMessagesViewsRequest(peer=other, id=[1], increment=False),
        functions.messages.GetMessagesViewsRequest(peer=other, id=[1], increment=True),
    ):
        try:
            check_request(views)  # no channel is configured: nothing is ours
        except RequestNotAllowed:
            continue
        raise AssertionError(f"getMessagesViews passed without our channel: {views}")
    assert MAX_HISTORY_LIMIT <= 10
    bot = types.InputPeerUser(user_id=93_372_553, access_hash=1)
    send = functions.messages.SendMessageRequest(peer=bot, message="/myproxies")
    check_request(send, bot_id=93_372_553)  # the bot's chat, once it is open
    for bot_id in (None, 777_000):  # chat not open on this connection, or another user id stored
        try:
            check_request(send, bot_id=bot_id)
        except RequestNotAllowed:
            continue
        raise AssertionError(f"sendMessage passed with bot_id={bot_id}")


def _tl_name(kind: type) -> str:
    return f"{kind.__module__.rsplit('.', 1)[-1]}.{kind.__name__}"


def test_the_operator_client_can_let_through_exactly_these_requests() -> None:
    """D38 adds exactly: a post and an edit in our channels, the account's own rights and the
    owner's emoji pack. Nothing that deletes, forwards or changes anything else, in any scope."""
    from proxyhub.mtproxy.userclient import (
        ALLOWED_REQUESTS,
        BOT_CHAT_REQUESTS,
        CHANNEL_POST_REQUESTS,
        POSTER_READ_REQUESTS,
    )

    passable = {_tl_name(k) for k in ALLOWED_REQUESTS | BOT_CHAT_REQUESTS | CHANNEL_POST_REQUESTS}
    assert passable == OPERATOR_PASSABLE, sorted(passable ^ OPERATOR_PASSABLE)
    assert {_tl_name(k) for k in CHANNEL_POST_REQUESTS} == {
        "messages.SendMessageRequest",
        "messages.EditMessageRequest",
    }
    assert {_tl_name(k) for k in POSTER_READ_REQUESTS} == {
        "channels.GetParticipantRequest",
        "messages.GetStickerSetRequest",
    }
    for name in passable:  # editing a POST is allowed; editing admins, titles or photos is not
        assert not FORBIDDEN_REQUEST_WORDS.search(name), name


def test_a_full_scope_still_refuses_every_delete_forward_and_media_request() -> None:
    """Even a connection allowed to post in the channel, read its rights and the emoji pack, with
    the bot chat open, cannot delete, forward, send media or post anywhere else."""
    from telethon import functions, types

    from proxyhub.mtproxy.userclient import ChannelScope, RequestNotAllowed, check_request

    ours = 1_234_567_890
    scope = ChannelScope(
        channel_ids=frozenset({ours}),
        usernames=frozenset({"ipoxxo"}),
        post_ids=frozenset({ours}),
        emoji_set="pack",
    )
    peer = types.InputPeerChannel(channel_id=ours, access_hash=1)
    channel = types.InputChannel(channel_id=ours, access_hash=1)
    check_request(functions.messages.SendMessageRequest(peer=peer, message="سلام"), scope)  # the control
    check_request(functions.messages.EditMessageRequest(peer=peer, id=7, message="سلام"), scope)
    for bad in (
        functions.messages.DeleteMessagesRequest(id=[7], revoke=True),
        functions.channels.DeleteMessagesRequest(channel=channel, id=[7]),
        functions.messages.DeleteHistoryRequest(peer=peer, max_id=0, revoke=True),
        functions.channels.DeleteHistoryRequest(channel=channel, max_id=0),
        functions.channels.DeleteChannelRequest(channel=channel),
        functions.messages.ForwardMessagesRequest(from_peer=peer, id=[7], to_peer=types.InputPeerSelf()),
        functions.messages.ForwardMessagesRequest(from_peer=types.InputPeerSelf(), id=[7], to_peer=peer),
        functions.messages.SendMediaRequest(peer=peer, media=types.InputMediaEmpty(), message="x"),
        functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message="x"),
        functions.messages.SendMessageRequest(
            peer=types.InputPeerChannel(channel_id=ours + 1, access_hash=1), message="x"
        ),
        functions.messages.EditMessageRequest(
            peer=types.InputPeerChannel(channel_id=ours + 1, access_hash=1), id=7, message="x"
        ),
        functions.channels.EditAdminRequest(
            channel=channel, user_id=types.InputUserSelf(), admin_rights=types.ChatAdminRights(), rank=""
        ),
        functions.channels.LeaveChannelRequest(channel=channel),
        functions.messages.UpdatePinnedMessageRequest(peer=peer, id=7),
    ):
        try:
            check_request(bad, scope, bot_id=93_372_553)
        except RequestNotAllowed:
            continue
        raise AssertionError(f"passed with a full scope: {type(bad).__name__}")
