"""The mtproto control loop, the links contract and the sponsor contract on a real PostgreSQL with
real cryptography (the agent API's own sealing, desired-state, ETag and report code).

The loop runs exactly as the worker runs it (``try_run_once``: advisory lock + one transaction).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_agent_support import reset_config_caches, use_secret_files, write_secret_files
from test_mtproto_support import (
    ZONE_NAME,
    actions,
    agent_reports,
    all_table_text,
    clean,
    current_etag,
    make_active_zone,
    make_node,
    make_slot_record,
    register_agent,
    rows,
    session_maker,
    set_node_state,
)

from proxyhub.agent_api.desired import (
    DesiredStateError,
    build_desired_state,
    create_telemt_user,
    reveal_secret,
    set_telemt_node_config,
    set_user_sponsor_tag,
)
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.mtproto.keys import SealKeyUnavailable
from proxyhub.mtproto.links import provide_links
from proxyhub.mtproto.settings import MtprotoSettings, save_settings
from proxyhub.mtproto.sponsor_api import UnknownProxy, apply_ad_tag, proxies_to_register
from proxyhub.mtproto.urls import extra_user_name, link_secret, proxy_key, user_name_for
from proxyhub.posts import settings as posting
from proxyhub.reconcilers.mtproto import MtprotoReconciler

pytestmark = pytest.mark.db

TAG_A = "a1" * 16
TAG_B = "b2" * 16


@dataclass
class Env:
    engine: AsyncEngine
    seal_key: bytes
    master_key: Path

    async def run(self, reconciler: MtprotoReconciler | None = None) -> MtprotoReconciler:
        rec = reconciler or MtprotoReconciler()
        assert await rec.try_run_once(self.engine)
        return rec

    async def secret(self, node_id: uuid.UUID) -> str:
        async with session_maker(self.engine)() as s:
            return await reveal_secret(s, self.seal_key, node_id, user_name_for(node_id))

    async def make_live(self, node_id: uuid.UUID) -> None:
        """What the node's agent does: apply the served state, then report it with Telemt up."""
        await register_agent(self.engine, node_id)
        await agent_reports(
            self.engine, node_id, applied_etag=await current_etag(self.engine, self.seal_key, node_id)
        )
        await self.run()

    async def settings(self, s: MtprotoSettings) -> None:
        async with self.engine.begin() as conn:
            await save_settings(conn, s)


@pytest.fixture
async def env(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    engine = create_async_engine(migrated_db)
    await clean(engine)
    await links_per_post(engine, 1)  # the primary user alone; the D49 tests below ask for more
    try:
        yield Env(engine=engine, seal_key=load_seal_key(files.master_key), master_key=files.master_key)
    finally:
        await clean(engine)
        await engine.dispose()
        reset_config_caches()


async def configs(engine: AsyncEngine) -> dict[uuid.UUID, Any]:
    return {
        r.node_id: r
        for r in await rows(
            engine,
            "SELECT node_id, ports, tls_domain, mode_tls, mode_secure, mode_classic, ad_tag "
            "FROM telemt_node_configs",
        )
    }


async def users(engine: AsyncEngine, node_id: uuid.UUID) -> list[Any]:
    return await rows(
        engine,
        "SELECT name, enabled, ad_tag, octet_length(secret_sealed) AS sealed, registration_id "
        "FROM telemt_users WHERE node_id = :n ORDER BY name",
        n=node_id,
    )


async def proxy_row(engine: AsyncEngine, node_id: uuid.UUID) -> Any:
    found = await rows(engine, "SELECT * FROM mtproto_proxies WHERE node_id = :n", n=node_id)
    return found[0] if found else None


async def links(env: Env) -> dict[str, Any]:
    async with env.engine.begin() as conn:
        return {link.key: link for link in await provide_links(conn)}


# --- the policy ------------------------------------------------------------------------------------


async def test_nodes_coming_up_or_in_service_get_telemt_settings_and_one_user(env: Env) -> None:
    states = [
        "requested", "creating", "booting", "registering", "ready", "unhealthy", "repairing",
        "burned", "draining", "retiring", "deleted",
    ]  # fmt: skip
    nodes = {state: await make_node(env.engine, state=state) for state in states}
    await env.run()
    got = await configs(env.engine)
    configured = {"booting", "registering", "ready", "unhealthy", "repairing"}
    assert {s for s, n in nodes.items() if n in got} == configured
    for state in configured:
        node = nodes[state]
        c = got[node]
        assert (c.ports, c.tls_domain, c.mode_tls, c.mode_secure, c.mode_classic, c.ad_tag) == (
            [8443],
            "www.microsoft.com",
            True,
            False,
            False,
            None,
        )
        assert [(u.name, u.enabled, u.ad_tag, u.sealed) for u in await users(env.engine, node)] == [
            (user_name_for(node), True, None, 45)
        ]
        p = await proxy_row(env.engine, node)
        assert p.user_name == user_name_for(node) and p.live_at is None and p.disabled_at is None
    for state in set(states) - configured:
        assert await users(env.engine, nodes[state]) == []
    done = await actions(env.engine)
    assert sorted(a["action"] for a in done) == sorted(["mtproto_config", "mtproto_user_created"] * 5)
    assert all(a["actor"] == "mtproto" and a["allowed"] for a in done)
    # Rule 3: the plaintext secrets are stored nowhere (only sealed), not even in actions_log.
    stored = await all_table_text(env.engine)
    for state in configured:
        secret = await env.secret(nodes[state])
        assert secret not in stored and secret.upper() not in stored


async def test_a_second_run_changes_nothing(env: Env) -> None:
    node = await make_node(env.engine)
    await env.run()
    secret, etag = await env.secret(node), await current_etag(env.engine, env.seal_key, node)
    before = await actions(env.engine)
    rec = await env.run()
    assert rec.last_outcome is not None and not rec.last_outcome.changed
    assert await actions(env.engine) == before
    assert await env.secret(node) == secret
    assert await current_etag(env.engine, env.seal_key, node) == etag


async def test_settings_change_rewrites_node_settings_and_keeps_secret_and_sponsor_tags(env: Env) -> None:
    node = await make_node(env.engine)
    burned = await make_node(env.engine, state="ready")
    await env.run()
    await set_node_state(env.engine, burned, "burned")
    secret = await env.secret(node)
    # A node-wide tag and a per-secret tag, as a sponsor would set them.
    async with session_maker(env.engine)() as s:
        await set_telemt_node_config(s, node, ports=[8443], tls_domain="www.microsoft.com", ad_tag=TAG_A)
        await s.commit()
    await env.make_live(node)
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, proxy_key(node, user_name_for(node)), TAG_B)
    await env.settings(MtprotoSettings(port=9443, tls_domain="www.apple.com", mode_secure=True))
    await env.run()
    c = (await configs(env.engine))[node]
    assert (c.ports, c.tls_domain, c.mode_tls, c.mode_secure, c.mode_classic, c.ad_tag) == (
        [9443],
        "www.apple.com",
        True,
        True,
        False,
        TAG_A,
    )
    assert [(u.name, u.ad_tag) for u in await users(env.engine, node)] == [(user_name_for(node), TAG_B)]
    assert await env.secret(node) == secret
    # A burned node keeps what it has.
    assert (await configs(env.engine))[burned].ports == [8443]
    last = [a for a in await actions(env.engine) if a["action"] == "mtproto_config"][-1]
    assert last["reasons"]["reasons"] == ["settings_changed"]
    assert last["reasons"]["context"]["port"] == 9443
    # The published link follows the node's settings.
    link = (await links(env))[proxy_key(node, user_name_for(node))]
    assert parse_qs(urlsplit(link.url).query) == {
        "server": ["203.0.113.10"],
        "port": ["9443"],
        "secret": [link_secret(secret, "tls", "www.apple.com")],
    }


async def test_a_proxy_is_live_only_once_its_agent_applied_the_current_state_with_telemt_up(env: Env) -> None:
    node = await make_node(env.engine)
    await register_agent(env.engine, node)
    before_user = await current_etag(env.engine, env.seal_key, node)  # no Telemt settings yet
    await env.run()
    assert (await proxy_row(env.engine, node)).live_at is None
    current = await current_etag(env.engine, env.seal_key, node)
    assert current != before_user
    for applied, ok, up in (
        (before_user, True, True),
        (current, False, True),
        (current, True, False),
        (None, True, True),
    ):
        await agent_reports(env.engine, node, applied_etag=applied, apply_ok=ok, telemt_up=up)
        await env.run()
        assert (await proxy_row(env.engine, node)).live_at is None, (applied, ok, up)
        assert await links(env) == {}
    await agent_reports(env.engine, node, applied_etag=current)
    await env.run()
    assert (await proxy_row(env.engine, node)).live_at is not None
    assert [a["action"] for a in await actions(env.engine)][-1] == "mtproto_proxy_live"
    # Later desired-state changes (a sponsor tag) do not take a live proxy back.
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, proxy_key(node, user_name_for(node)), TAG_A)
    await env.run()
    assert (await proxy_row(env.engine, node)).live_at is not None


async def test_only_a_ready_node_goes_live(env: Env) -> None:
    node = await make_node(env.engine, state="registering")
    await env.run()
    await env.make_live(node)
    assert (await proxy_row(env.engine, node)).live_at is None
    await set_node_state(env.engine, node, "ready")
    await env.run()
    assert (await proxy_row(env.engine, node)).live_at is not None


async def test_secrets_of_nodes_leaving_service_are_disabled(env: Env) -> None:
    nodes = {s: await make_node(env.engine) for s in ("retiring", "deleted", "draining", "burned")}
    await env.run()
    for state, node in nodes.items():
        await env.make_live(node)
        async with session_maker(env.engine)() as s:  # another secret on the node (e.g. a sponsor's)
            await create_telemt_user(s, env.seal_key, node, "sponsor_b", ad_tag=TAG_B)
            await s.commit()
        await set_node_state(env.engine, node, state)
    await env.run()
    for state, node in nodes.items():
        enabled = [u.enabled for u in await users(env.engine, node)]
        p = await proxy_row(env.engine, node)
        if state in ("retiring", "deleted"):
            assert enabled == [False, False], state
            assert p.disabled_at is not None
        else:
            assert enabled == [True, True], state
            assert p.disabled_at is None
    disabled = [a for a in await actions(env.engine) if a["action"] == "mtproto_users_disabled"]
    assert sorted(a["reasons"]["reasons"][0] for a in disabled) == ["node_deleted", "node_retiring"]
    assert all(a["reasons"]["context"]["users"] == 2 for a in disabled)
    # The desired state a retiring node's agent still pulls has the users off.
    async with session_maker(env.engine)() as s:
        doc = await build_desired_state(s, env.seal_key, nodes["retiring"])
    assert [u["enabled"] for u in doc["telemt"]["users"]] == [False, False]
    # Idempotent: nothing more to disable.
    count = len(await actions(env.engine))
    await env.run()
    assert len(await actions(env.engine)) == count


async def test_without_valid_settings_or_master_key_nothing_is_made_but_retiring_still_disables(
    env: Env,
) -> None:
    leaving = await make_node(env.engine)
    await env.run()
    await set_node_state(env.engine, leaving, "retiring")
    fresh = await make_node(env.engine)
    async with env.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('mtproto', CAST(:v AS jsonb))"),
            {"v": '{"port": 443}'},
        )
    await env.run()
    assert fresh not in await configs(env.engine)
    assert [u.enabled for u in await users(env.engine, leaving)] == [False]

    def no_key() -> bytes:
        raise SealKeyUnavailable("missing")

    async with env.engine.begin() as conn:
        await conn.execute(text("DELETE FROM settings WHERE key = 'mtproto'"))
    await env.run(MtprotoReconciler(seal_key_loader=no_key))
    assert fresh not in await configs(env.engine) and await users(env.engine, fresh) == []
    await env.run()
    assert fresh in await configs(env.engine)


async def test_one_broken_node_neither_blocks_the_others_nor_the_disabling(env: Env) -> None:
    broken, fine, leaving = [await make_node(env.engine) for _ in range(3)]
    await env.run()
    await env.make_live(leaving)
    await set_node_state(env.engine, leaving, "retiring")
    # broken: READY but not yet live, with a sealed secret that no longer opens (e.g. after a master
    # key mismatch), so building its desired state raises SealError on every pass.
    await register_agent(env.engine, broken)
    etag = await current_etag(env.engine, env.seal_key, broken)
    await agent_reports(env.engine, broken, applied_etag=etag)  # applied, Telemt up
    await set_node_state(env.engine, broken, "ready")
    async with env.engine.begin() as conn:
        await conn.execute(
            # Flip one ciphertext byte: same length, but AES-GCM no longer opens it.
            text(
                "UPDATE telemt_users SET secret_sealed = "
                "set_byte(secret_sealed, 20, get_byte(secret_sealed, 20) # 255) WHERE node_id = :n"
            ),
            {"n": broken},
        )
    await env.run()
    assert [u.enabled for u in await users(env.engine, leaving)] == [False]
    assert fine in await configs(env.engine) and [u.enabled for u in await users(env.engine, fine)] == [True]
    assert (await proxy_row(env.engine, broken)).live_at is None


async def test_a_failing_run_leaves_nothing_behind(env: Env) -> None:
    node = await make_node(env.engine)
    with pytest.raises(ValueError):  # AES-GCM refuses the key while sealing the new secret
        await MtprotoReconciler(seal_key_loader=lambda: b"short").try_run_once(env.engine)
    assert await configs(env.engine) == {}
    assert await users(env.engine, node) == [] and await proxy_row(env.engine, node) is None
    assert await actions(env.engine) == []


async def test_a_removed_user_is_made_again_with_a_new_secret(env: Env) -> None:
    node = await make_node(env.engine)
    await env.run()
    await env.make_live(node)
    old = await env.secret(node)
    async with env.engine.begin() as conn:
        await conn.execute(text("DELETE FROM telemt_users WHERE node_id = :n"), {"n": node})
    await env.run()
    assert await env.secret(node) != old
    p = await proxy_row(env.engine, node)
    assert p.live_at is None and p.disabled_at is None
    assert [
        a["reasons"]["reasons"] for a in await actions(env.engine) if a["action"] == "mtproto_user_created"
    ][-1] == ["user_missing"]


async def test_other_users_on_a_node_are_left_alone(env: Env) -> None:
    node = await make_node(env.engine)
    async with session_maker(env.engine)() as s:
        await create_telemt_user(s, env.seal_key, node, "customer_x", ad_tag=TAG_A, max_tcp_conns=7)
        await s.commit()
    await env.run()
    await env.settings(MtprotoSettings(port=9443))
    await env.run()
    got = await rows(
        env.engine,
        "SELECT name, enabled, ad_tag, max_tcp_conns FROM telemt_users WHERE node_id = :n ORDER BY name",
        n=node,
    )
    assert [tuple(r) for r in got] == [
        ("customer_x", True, TAG_A, 7),
        (user_name_for(node), True, None, None),
    ]


# --- links contract --------------------------------------------------------------------------------


async def test_links_hosts_status_and_lifetime(env: Env) -> None:
    await make_active_zone(env.engine)
    one = await make_node(env.engine, slot=1, ipv4="203.0.113.1")
    two = await make_node(env.engine, slot=2, ipv4="203.0.113.2")
    test = await make_node(env.engine, slot=None, ipv4="203.0.113.3", is_test=True)
    pending = await make_node(env.engine, slot=4, ipv4="203.0.113.4")
    burned = await make_node(env.engine, slot=5, ipv4="203.0.113.5")
    gone = await make_node(env.engine, slot=6, ipv4="203.0.113.6")
    old = await make_node(env.engine, slot=7, ipv4="203.0.113.7")
    await make_slot_record(env.engine, one, 1, "203.0.113.1")
    await make_slot_record(env.engine, two, 2, "203.0.113.2", created=False)  # not yet in Cloudflare
    await make_slot_record(env.engine, burned, 5, "203.0.113.99")  # still points at another IP
    await env.run()
    for node in (one, two, test, burned, gone, old):
        await env.make_live(node)
    await set_node_state(env.engine, burned, "burned")
    await set_node_state(env.engine, gone, "deleted")
    await set_node_state(env.engine, old, "deleted")
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE nodes SET deleted_at = :at WHERE id = :n"),
            {"at": datetime.now(UTC) - timedelta(days=8), "n": old},
        )
    await env.run()
    got = await links(env)
    by_node = {uuid.UUID(k.split(":")[1]): v for k, v in got.items()}
    assert set(by_node) == {one, two, test, burned, gone}  # never-live and long-deleted are not listed
    assert list(by_node) == [one, two, burned, gone, test]  # slot order, slotless last
    expect = {
        one: (f"slot1.{ZONE_NAME}", 1, "پروکسی ۱", "active"),
        two: ("203.0.113.2", 2, "پروکسی ۲", "active"),
        burned: ("203.0.113.5", 5, "پروکسی ۵", "burned"),
        gone: ("203.0.113.6", 6, "پروکسی ۶", "expired"),
        test: ("203.0.113.3", None, "پروکسی آزمایشی", "active"),
    }
    for node, (host, slot, label, status) in expect.items():
        link = by_node[node]
        assert (link.kind, link.slot, link.label, link.status, link.expires_at) == (
            "mtproto",
            slot,
            label,
            status,
            None,
        )
        assert link.key == proxy_key(node, user_name_for(node))
        assert parse_qs(urlsplit(link.url).query) == {
            "server": [host],
            "port": ["8443"],
            "secret": [link_secret(await env.secret(node), "tls", "www.microsoft.com")],
        }
    assert pending not in by_node


async def test_links_need_the_master_key(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    node = await make_node(env.engine)
    await env.run()
    await env.make_live(node)
    monkeypatch.setenv("PROXYHUB_MASTER_KEY_FILE", str(env.master_key.parent / "missing"))
    reset_config_caches()
    with pytest.raises(SealKeyUnavailable):
        await links(env)


# --- sponsor contract ------------------------------------------------------------------------------


async def registration(env: Env, secret_name: str, state: str, ad_tag: str | None = None) -> uuid.UUID:
    reg_id = uuid.uuid4()
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mtproxy_registrations (id, host, port, secret_name, ad_tag, state) "
                "VALUES (:id, :host, 8443, :name, :tag, :state)"
            ),
            {
                "id": reg_id,
                "host": f"{reg_id.hex[:8]}.test-reg",
                "name": secret_name,
                "tag": ad_tag,
                "state": state,
            },
        )
    return reg_id


async def test_proxies_to_register_are_live_proxies_in_service_without_a_registration(env: Env) -> None:
    names = ["plain", "pending", "burned", "unhealthy", "registered"]
    names += ["pending_reg", "failed_reg", "by_key", "tagged"]
    nodes = {
        n: await make_node(env.engine, slot=i + 1, ipv4=f"203.0.113.{i + 1}") for i, n in enumerate(names)
    }
    await env.run()
    for name, node in nodes.items():
        if name != "pending":
            await env.make_live(node)
    await set_node_state(env.engine, nodes["burned"], "burned")
    await set_node_state(env.engine, nodes["unhealthy"], "unhealthy")  # registered once it is ready again
    await registration(env, user_name_for(nodes["registered"]), "registered", TAG_A)
    await registration(env, user_name_for(nodes["pending_reg"]), "pending")
    await registration(env, user_name_for(nodes["failed_reg"]), "failed")
    await registration(env, proxy_key(nodes["by_key"], user_name_for(nodes["by_key"])), "registered")
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, proxy_key(nodes["tagged"], user_name_for(nodes["tagged"])), TAG_B)
    async with env.engine.begin() as conn:
        todo = await proxies_to_register(conn)
    assert [p.key for p in todo] == [
        proxy_key(nodes[n], user_name_for(nodes[n])) for n in ("plain", "failed_reg")
    ]
    first = todo[0]
    assert (first.host, first.port) == ("203.0.113.1", 8443)
    assert first.secret_hex == await env.secret(nodes["plain"]) and len(first.secret_hex) == 32
    assert first.secret_hex not in repr(first)


async def test_apply_ad_tag_puts_the_tag_on_the_proxys_own_user(env: Env) -> None:
    node = await make_node(env.engine)
    await env.run()
    await env.make_live(node)
    key = proxy_key(node, user_name_for(node))
    reg = await registration(env, user_name_for(node), "registered", TAG_A)
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, key, TAG_A)
    async with session_maker(env.engine)() as s:
        doc = await build_desired_state(s, env.seal_key, node)
    assert [(u["name"], u["ad_tag"]) for u in doc["telemt"]["users"]] == [(user_name_for(node), TAG_A)]
    assert doc["telemt"]["ad_tag"] is None  # node-wide tag untouched
    assert [u.registration_id for u in await users(env.engine, node)] == [reg]
    # Counters of a registered proxy now reach mtproxy_stats.
    await agent_reports(
        env.engine,
        node,
        applied_etag=await current_etag(env.engine, env.seal_key, node),
        users=[{"name": user_name_for(node), "current_connections": 4, "total_octets": 1000}],
    )
    assert [
        tuple(r)
        for r in await rows(
            env.engine, "SELECT current_connections FROM mtproxy_stats WHERE registration_id = :r", r=reg
        )
    ] == [(4,)]
    logged = [a for a in await actions(env.engine) if a["action"] == "mtproto_ad_tag"]
    assert logged[-1]["reasons"]["reasons"] == ["tag_set"]
    assert TAG_A not in str(logged) and TAG_A not in str(await actions(env.engine))
    # Clearing it, inside a transaction the caller rolls back: nothing changes.
    async with env.engine.connect() as conn:
        await conn.begin()
        await apply_ad_tag(conn, key, None)
        await conn.rollback()
    assert [u.ad_tag for u in await users(env.engine, node)] == [TAG_A]
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, key, None)
    assert [(u.ad_tag, u.registration_id) for u in await users(env.engine, node)] == [(None, None)]
    # Inside the caller's SAVEPOINT the tag is kept too (the savepoint and the transaction commit).
    async with env.engine.begin() as conn:
        async with conn.begin_nested():
            await apply_ad_tag(conn, key, TAG_A)
    assert [(u.ad_tag, u.registration_id) for u in await users(env.engine, node)] == [(TAG_A, reg)]
    # And a SAVEPOINT the caller rolls back takes it away again, with the registration link.
    async with env.engine.begin() as conn:
        nested = await conn.begin_nested()
        await apply_ad_tag(conn, key, None)
        await nested.rollback()
    assert [(u.ad_tag, u.registration_id) for u in await users(env.engine, node)] == [(TAG_A, reg)]


@pytest.mark.parametrize("tag", ["A1" * 16, "a1" * 15, "x" * 32, ""])
async def test_apply_ad_tag_refuses_a_malformed_tag(env: Env, tag: str) -> None:
    node = await make_node(env.engine)
    await env.run()
    with pytest.raises(DesiredStateError):
        async with env.engine.begin() as conn:
            await apply_ad_tag(conn, proxy_key(node, user_name_for(node)), tag)


async def test_apply_ad_tag_refuses_unknown_proxies(env: Env) -> None:
    node = await make_node(env.engine)
    await env.run()
    async with session_maker(env.engine)() as s:
        await create_telemt_user(s, env.seal_key, node, "customer_x")
        await s.commit()
    for key in (
        "garbage",
        f"reality:{node}:{user_name_for(node)}",
        proxy_key(uuid.uuid4(), user_name_for(node)),
        proxy_key(node, "customer_x"),  # a user that is not the node's public proxy
    ):
        with pytest.raises(UnknownProxy):
            async with env.engine.begin() as conn:
                await apply_ad_tag(conn, key, TAG_A)
    assert {u.name: u.ad_tag for u in await users(env.engine, node)} == {
        "customer_x": None,
        user_name_for(node): None,
    }


# --- extra links on a node (decision D49) ------------------------------------------------------------


async def links_per_post(engine: AsyncEngine, n: int) -> None:
    """The posting setting ``max_mtproto`` the loop sizes the extras by."""
    async with engine.begin() as conn:
        await posting.save(conn, posting.parse({**posting.DEFAULTS.as_json(), "max_mtproto": n}))


async def proxies(engine: AsyncEngine, node_id: uuid.UUID) -> list[tuple[str, int]]:
    found = await rows(
        engine,
        "SELECT user_name, ordinal FROM mtproto_proxies WHERE node_id = :n ORDER BY ordinal",
        n=node_id,
    )
    return [(r.user_name, r.ordinal) for r in found]


def all_links(node: uuid.UUID, n: int) -> list[tuple[str, int]]:
    return [(user_name_for(node), 0)] + [(extra_user_name(node, k), k) for k in range(1, n)]


async def test_extras_fill_a_post_while_few_nodes_run_and_stop_at_the_cap(env: Env) -> None:
    await links_per_post(env.engine, 6)
    a = await make_node(env.engine, slot=1)
    await env.run()
    assert await proxies(env.engine, a) == all_links(a, 6)  # one node: its primary + 5 extras
    found = await users(env.engine, a)
    assert len(found) == 6 and all(u.sealed == 45 and u.enabled for u in found)
    async with session_maker(env.engine)() as s:
        secrets = {await reveal_secret(s, env.seal_key, a, u.name) for u in found}
    assert len(secrets) == 6  # each extra has its own secret
    async with session_maker(env.engine)() as s:
        doc = await build_desired_state(s, env.seal_key, a)
    assert [u["name"] for u in doc["telemt"]["users"]] == sorted(name for name, _ in all_links(a, 6))
    # A second slot node: the two share a post (3 links each); the first keeps all of its links.
    b = await make_node(env.engine, slot=2, ipv4="203.0.113.11")
    await env.run()
    assert await proxies(env.engine, b) == all_links(b, 3) and await proxies(env.engine, a) == all_links(a, 6)
    assert not (await env.run()).last_outcome.changed  # nothing more to do
    # Never more than six links on a node, whatever the setting.
    await links_per_post(env.engine, 20)
    await env.run()
    assert await proxies(env.engine, a) == all_links(a, 6) and await proxies(env.engine, b) == all_links(b, 6)
    made = [x for x in await actions(env.engine) if x["action"] == "mtproto_extra_users_created"]
    assert [m["reasons"]["context"]["links"] for m in made] == [6, 3, 6]
    text_dump = await all_table_text(env.engine)
    for secret in secrets:  # rule 3: no secret outside the sealed column
        assert secret not in text_dump


async def test_a_test_node_gets_extras_only_while_no_slot_node_runs(env: Env) -> None:
    await links_per_post(env.engine, 6)
    t = await make_node(env.engine, is_test=True)
    await env.run()
    assert len(await proxies(env.engine, t)) == 6  # alone (test channel): it fills the post
    s1 = await make_node(env.engine, slot=1, ipv4="203.0.113.11")
    await env.run()
    assert len(await proxies(env.engine, s1)) == 6  # the slot node fills a main-channel post
    assert len(await proxies(env.engine, t)) == 6  # what the test node has, it keeps
    t2 = await make_node(env.engine, is_test=True, ipv4="203.0.113.12")
    await env.run()
    assert await proxies(env.engine, t2) == all_links(t2, 1)  # slot nodes run: no extras for it


async def test_extras_are_live_links_never_offered_for_registration_and_follow_the_tag(env: Env) -> None:
    await links_per_post(env.engine, 3)
    node = await make_node(env.engine, slot=1)
    await env.run()
    await env.make_live(node)
    primary = proxy_key(node, user_name_for(node))
    extra_keys = [proxy_key(node, extra_user_name(node, k)) for k in (1, 2)]
    got = await links(env)
    assert set(got) == {primary, *extra_keys}
    assert all(got[k].status == "active" and got[k].extra_of == primary for k in extra_keys)
    assert got[primary].extra_of is None and got[extra_keys[0]].label == "پروکسی ۱، لینک ۲"
    assert len({got[k].url for k in got}) == 3  # same server and port, three secrets
    async with env.engine.begin() as conn:
        assert [p.key for p in await proxies_to_register(conn)] == [primary]  # never an extra
    # The sponsor's tag on the primary reaches the extras at once, with the registration link.
    reg = await registration(env, user_name_for(node), "registered", TAG_A)
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, primary, TAG_A)
    assert [(u.ad_tag, u.registration_id) for u in await users(env.engine, node)] == [(TAG_A, reg)] * 3
    async with env.engine.begin() as conn:
        assert await proxies_to_register(conn) == []
    with pytest.raises(UnknownProxy):  # an extra has no tag of its own
        async with env.engine.begin() as conn:
            await apply_ad_tag(conn, extra_keys[0], TAG_B)
    # Re-registered on the primary by other code: the loop gives the extras the new tag.
    async with session_maker(env.engine)() as s:
        await set_user_sponsor_tag(s, node, user_name_for(node), TAG_B)
        await s.commit()
    outcome = (await env.run()).last_outcome
    assert outcome is not None and outcome.tags_synced == {node: 2}
    assert {u.ad_tag for u in await users(env.engine, node)} == {TAG_B}
    async with env.engine.begin() as conn:
        await apply_ad_tag(conn, primary, None)  # cleared: the extras lose it too
    assert {(u.ad_tag, u.registration_id) for u in await users(env.engine, node)} == {(None, None)}
    assert TAG_A not in str(await actions(env.engine)) and TAG_B not in str(await actions(env.engine))
    # Per-secret counters (D39) are kept for an extra like for the primary.
    await agent_reports(
        env.engine,
        node,
        applied_etag=await current_etag(env.engine, env.seal_key, node),
        users=[{"name": extra_user_name(node, 2), "current_connections": 7, "total_octets": 900}],
    )
    counted = await rows(
        env.engine,
        "SELECT name, last_current_connections AS c FROM telemt_users WHERE node_id = :n ORDER BY name",
        n=node,
    )
    assert [(r.name, r.c) for r in counted] == [(user_name_for(node), None), (extra_user_name(node, 1), None),
                                                (extra_user_name(node, 2), 7)]  # fmt: skip


async def test_an_extra_is_kept_while_its_node_lives_and_goes_with_it(env: Env) -> None:
    await links_per_post(env.engine, 6)
    a = await make_node(env.engine, slot=1)
    await env.run()
    await env.make_live(a)
    # More nodes come: a needs no extras any more, but keeps them (its links are in posts).
    others = [await make_node(env.engine, slot=i, ipv4=f"203.0.113.{20 + i}") for i in range(2, 7)]
    await env.run()
    assert await proxies(env.engine, a) == all_links(a, 6)
    for n in others:
        assert await proxies(env.engine, n) == all_links(n, 1)
    assert all(u.enabled for u in await users(env.engine, a))
    got = await links(env)
    assert sum(1 for x in got.values() if x.extra_of is not None and x.status == "active") == 5
    # The node leaves service: its extras are disabled with its primary, and listed as ended.
    await set_node_state(env.engine, a, "retiring")
    await env.run()
    assert not any(u.enabled for u in await users(env.engine, a))
    gone = await rows(env.engine, "SELECT count(*) AS n FROM mtproto_proxies WHERE node_id = :n "
                                  "AND disabled_at IS NOT NULL", n=a)  # fmt: skip
    assert gone[0].n == 6
    got = await links(env)
    assert {x.status for k, x in got.items() if k.startswith(f"mtproto:{a}:")} == {"expired"}
    assert len([k for k in got if k.startswith(f"mtproto:{a}:")]) == 6


def test_links_per_node() -> None:
    from proxyhub.mtproto.policy import links_per_node

    assert [links_per_node(6, slotted=n, unslotted=0, has_slot=True) for n in range(1, 8)] == [
        6,
        3,
        2,
        2,
        2,
        1,
        1,
    ]
    assert links_per_node(20, slotted=1, unslotted=0, has_slot=True) == 6  # the cap per node
    assert links_per_node(1, slotted=1, unslotted=0, has_slot=True) == 1
    assert links_per_node(6, slotted=0, unslotted=2, has_slot=False) == 3  # test nodes alone
    assert links_per_node(6, slotted=1, unslotted=2, has_slot=False) == 1  # slot nodes fill the post
    assert links_per_node(6, slotted=0, unslotted=0, has_slot=True) == 1
