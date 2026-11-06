"""The sponsor module against team mtproto's REAL provider (``proxyhub.mtproto.sponsor_api``), on a
real PostgreSQL with real sealing. It runs where both teams' code is on one branch (the round-3
integration) and is skipped where team mtproto's code is absent (this team's own branch, whose
tests use :class:`test_sponsor_support.TelemtUsersProvider`, written to the same rule).

It pins the one meaning both sides agree on (cross-team SPONSOR contract): ``proxies_to_register``
lists a live proxy until it has a registration, and this module writes the registration row only
once @MTProxybot gave the tag. So the real provider keeps listing a proxy through passes without a
conversation, through failed tries and after «ثبت دوباره», stops listing it once it has its tag,
and the tag lands on the proxy's own Telemt user, linked to that registration row.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

sponsor_api: Any = pytest.importorskip("proxyhub.mtproto.sponsor_api")
mtproto_rec: Any = pytest.importorskip("proxyhub.reconcilers.mtproto")
mtp: Any = pytest.importorskip("test_mtproto_support")

from test_agent_support import reset_config_caches, use_secret_files, write_secret_files  # noqa: E402
from test_fleet_support import make_vault  # noqa: E402
from test_mtproxy_support import seed_connected  # noqa: E402
from test_mtproxy_units import closed_port  # noqa: E402
from test_sponsor_support import clean, kill_switch, registration_rows, regs  # noqa: E402

from proxyhub.agent_api.sealing import load_seal_key  # noqa: E402
from proxyhub.mtproxy.userclient import ClientOptions, Endpoint  # noqa: E402
from proxyhub.sponsor import service, store  # noqa: E402
from proxyhub.sponsor.conversation import Registered, RegisterJob, Step, Stuck  # noqa: E402
from proxyhub.sponsor.driver import FlowResult  # noqa: E402
from proxyhub.sponsor.service import Deps, Work  # noqa: E402
from proxyhub.vault import Vault  # noqa: E402

pytestmark = [pytest.mark.db]

BOT_ID = 93_372_553
TAG = "ecfe4482355b04af36e816044653e999"
NOON = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@dataclass
class Env:
    engine: AsyncEngine
    seal_key: bytes
    vault: Vault

    async def live_node(self, slot: int) -> uuid.UUID:
        """A node in service whose agent applied the served Telemt state (team mtproto's loop)."""
        node = await mtp.make_node(self.engine, slot=slot, ipv4=f"203.0.113.{60 + slot}")
        rec = mtproto_rec.MtprotoReconciler()
        assert await rec.try_run_once(self.engine)
        await mtp.register_agent(self.engine, node)
        etag = await mtp.current_etag(self.engine, self.seal_key, node)
        await mtp.agent_reports(self.engine, node, applied_etag=etag)
        assert await rec.try_run_once(self.engine)
        return node

    async def listed(self) -> list[str]:
        async with self.engine.begin() as conn:
            return sorted(p.key for p in await sponsor_api.proxies_to_register(conn))

    async def work(self, reg_id: uuid.UUID) -> Work:
        async with self.engine.begin() as conn:
            live = {p.key: p for p in await sponsor_api.proxies_to_register(conn)}
            reg = await store.load_reg(conn, reg_id)
        assert reg is not None
        job = RegisterJob(host=reg.host, port=reg.port, secret_hex=live[str(reg.proxy_key)].secret_hex)
        return Work(reg=reg, job=job, key=reg.proxy_key, priority=1)


@pytest.fixture
async def env(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    engine = create_async_engine(migrated_db)
    await clean(engine)
    await mtp.clean(engine)
    try:
        yield Env(engine=engine, seal_key=load_seal_key(files.master_key), vault=make_vault(tmp_path))
    finally:
        await clean(engine)
        await mtp.clean(engine)
        await engine.dispose()
        reset_config_caches()


def deps(env: Env, clock: list[datetime]) -> Deps:
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=3)
    return Deps(
        engine=env.engine, vault=env.vault, provider=sponsor_api, options=options, clock=lambda: clock[0]
    )


async def test_the_real_provider_keeps_listing_a_proxy_until_it_has_its_tag(env: Env) -> None:
    await kill_switch(env.engine, False)
    a, b = await env.live_node(1), await env.live_node(2)
    both = await env.listed()
    assert len(both) == 2 and all(str(n) in " ".join(both) for n in (a, b))
    clock = [NOON]
    d = deps(env, clock)
    # A pass with nobody to talk to: two candidates, no registration row, both still listed.
    assert await service.run_once(d) == "no_account"
    assert await registration_rows(env.engine) == [] and await env.listed() == both
    # Telegram unreachable: one try per pass, each fails; both stay listed and are tried again.
    await seed_connected(env.engine, env.vault, port=closed_port(), mtproxybot_id=BOT_ID)
    assert [await service.run_once(d) for _ in range(2)] == ["failed:telegram_unavailable"] * 2
    assert await env.listed() == both
    clock[0] += timedelta(minutes=6)
    assert [await service.run_once(d) for _ in range(2)] == ["failed:telegram_unavailable"] * 2
    assert sorted(r["attempts"] for r in await regs(env.engine)) == [2, 2]
    # The bot gives one of them its tag: the registration row exists now, the real provider stops
    # listing that proxy, and the tag sits on its own Telemt user, linked to the row.
    first, second = sorted(await regs(env.engine), key=lambda r: r["host"])
    outcome = FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET)
    assert await service._record(d, await env.work(first["id"]), outcome) == "registered"
    (written,) = await registration_rows(env.engine)
    assert written["state"] == "registered" and written["ad_tag"] == TAG
    assert await env.listed() == [second["proxy_key"]]
    users = await mtp.rows(
        env.engine, "SELECT name, ad_tag, registration_id FROM telemt_users WHERE node_id = :n", n=a
    )
    # Node a ran alone at first, so it keeps five extra links (D49): they carry the same tag and
    # registration, and none of them was ever listed for a registration of its own.
    assert len(users) == 6 and {(u.ad_tag, u.registration_id) for u in users} == {(TAG, written["id"])}
    # The other gets stuck on a reply nobody understands, and «ثبت دوباره» tries it again.
    stuck = FlowResult(result=Stuck(step=Step.SENT_ADDRESS, reason="address_refused"))
    assert await service._record(d, await env.work(second["id"]), stuck) == "stuck:address_refused"
    assert await env.listed() == [second["proxy_key"]]  # stuck, and still listed with its secret
    await service.retry(env.engine, second["id"], actor="admin:owner", now=clock[0])
    assert await service.run_once(d) == "failed:telegram_unavailable"
    assert await env.listed() == [second["proxy_key"]]
    assert secrets.compare_digest(TAG, written["ad_tag"])
