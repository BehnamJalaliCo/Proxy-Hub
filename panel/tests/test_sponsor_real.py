"""REAL conversations with @MTProxybot as the operator account (marker ``telegram_user``), run on the
panel server only (this sandbox cannot carry raw MTProto). Decision D37: the SECOND account.

Environment (skipped without it, tests/conftest.py):
  PROXYHUB_TELEGRAM_USER_TEST_FILE  a 0600 file outside the repo (the same file as
      tests/test_mtproxy_real.py): ``{"api_id", "api_hash", "session"}`` runs the read-only test
      (one ``/myproxies``, its reply recorded for docs/11 F11). Adding
      ``"proxy": {"host": "...", "port": 8443, "secret": "<32 hex>"}`` (a LIVE proxy of this panel)
      also runs the full registration and "Set promotion" to @ipoxxo (or ``"channel"``): this
      creates a real entry in @MTProxybot, so it is run once, on purpose, from the runbook.

Every message goes through the real pacing (5 s gap, 30 an hour) and is written to actions_log;
the Telethon loggers run at DEBUG and no secret may reach a log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_sponsor_support import actions, clean, kill_switch
from test_telegram_support import all_logs

from proxyhub.config import read_secret_file
from proxyhub.mtproxy.userclient import ApiCredentials, operator_session
from proxyhub.sponsor import driver, pacing, replies
from proxyhub.sponsor.conversation import PromoteJob, PromotionSet, Registered, RegisterJob
from proxyhub.sponsor.service import _Recorder
from proxyhub.vault import SecretValue

pytestmark = [pytest.mark.telegram_user, pytest.mark.db]


def _file() -> dict[str, Any]:
    path = Path(os.environ.get("PROXYHUB_TELEGRAM_USER_TEST_FILE", ""))
    if path.stat().st_mode & 0o077:
        pytest.fail(f"{path} must be readable by its owner only (chmod 600)")
    data = json.loads(read_secret_file(path))
    assert isinstance(data, dict)
    if not {"api_id", "api_hash", "session"} <= set(data):
        pytest.skip("the test file holds no operator-account session")
    return data


def _account(data: dict[str, Any]) -> tuple[ApiCredentials, SecretValue]:
    return (
        ApiCredentials(api_id=int(data["api_id"]), api_hash=SecretValue(str(data["api_hash"]))),
        SecretValue(str(data["session"])),
    )


@pytest.fixture
def telethon_debug() -> Iterator[None]:
    logger = logging.getLogger("telethon")
    old = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.setLevel(old)


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean(eng)
    await kill_switch(eng, False)
    yield eng
    await clean(eng)
    await eng.dispose()


def _now() -> datetime:
    return datetime.now(UTC)


async def test_myproxies_is_answered_with_the_published_list_header(
    engine: AsyncEngine, telethon_debug: None
) -> None:
    data = _file()
    creds, session = _account(data)
    with all_logs(logging.DEBUG) as logs:
        async with operator_session(creds, session) as op:
            bot_id = await op.resolve_mtproxybot()
            chat = await op.open_mtproxybot_chat(bot_id)
            before = await chat.recent()
            await pacing.take_slot(engine, clock=_now)
            await chat.send("/myproxies")
            found, _ = await driver._await_reply(chat, before, driver.Timing(), asyncio.sleep)
    text = replies.mask(replies.joined(found))
    # docs/11 F11: the real reply, masked, for the record (shown with -rA).
    print(json.dumps({"reply": text, "buttons": [replies.mask(b) for b in driver.button_labels(found)]}))
    assert found, "@MTProxybot did not answer /myproxies"
    assert replies.is_proxy_list(text), f"the /myproxies header changed: {text!r}"
    for secret in (creds.api_hash.reveal(), session.reveal()):
        assert secret not in logs.getvalue()


async def test_a_live_proxy_is_registered_and_promoted_for_real(
    engine: AsyncEngine, telethon_debug: None
) -> None:
    data = _file()
    proxy = data.get("proxy")
    if not isinstance(proxy, dict):
        pytest.skip("no live proxy in the test file (a real registration is run on purpose only)")
    creds, session = _account(data)
    register = RegisterJob(host=str(proxy["host"]), port=int(proxy["port"]), secret_hex=str(proxy["secret"]))
    channel = str(data.get("channel", "ipoxxo"))
    timing = driver.Timing(reply_timeout_s=45)

    async def slot() -> object:
        return await pacing.take_slot(engine, clock=_now)

    with all_logs(logging.DEBUG) as logs:
        async with operator_session(creds, session) as op:
            chat = await op.open_mtproxybot_chat(await op.resolve_mtproxybot())
            got = await driver.run(
                chat, register, take_slot=slot, record=_Recorder(engine, register.address), timing=timing
            )
            assert isinstance(got.result, Registered), got
            promote = PromoteJob(host=register.host, port=register.port, channel=channel)
            done = await driver.run(
                chat, promote, take_slot=slot, record=_Recorder(engine, register.address), timing=timing
            )
    assert isinstance(done.result, PromotionSet), done
    print(
        json.dumps(
            {"tag": replies.short_tag(got.result.tag), "steps": [a["action"] for a in await actions(engine)]}
        )
    )
    for secret in (register.secret_hex, creds.api_hash.reveal(), session.reveal()):
        assert secret not in logs.getvalue()
    assert register.secret_hex not in json.dumps([a["reasons"] for a in await actions(engine)])
