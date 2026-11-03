"""Owner alerts: Alertmanager webhook route over real HTTP (ASGI), rendering from the bodies the real
Alertmanager sent (tests/test_telegram_captured.py), dedupe, retry and the database-down cache."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import make_vault
from test_telegram_captured import (
    FIRING_DISK_TWO,
    FIRING_WORKER_STALE,
    MIXED_REDIS,
    RESOLVED_DISK_TWO,
    RESOLVED_WORKER_STALE,
)
from test_telegram_support import (
    all_logs,
    assert_no_token,
    clean_telegram_tables,
    closed_port_bot,
    random_token,
    real_bot,
    telegram_reachable,
)

from proxyhub.api.routes_telegram import get_bot_factory, get_vault
from proxyhub.telegram import store
from proxyhub.telegram.alerts import (
    ALERT_DEDUPER,
    ALERT_TARGETS,
    AlertmanagerWebhook,
    AmAlert,
    Deduper,
    NotConfigured,
    OwnerTarget,
    TargetCache,
    render_alertmanager_message,
    send_direct,
)
from proxyhub.telegram.client import NetworkError
from proxyhub.telegram.templates import MAX_MESSAGE_CHARS, utf16_len, visible_text
from proxyhub.vault import SecretValue, Vault

ROUTE = "/internal/alerts/alertmanager"


def hook(raw: str) -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate_json(raw)


# --- rendering (pure) ----------------------------------------------------------------------------


def test_firing_message_from_the_real_body() -> None:
    text = render_alertmanager_message(hook(FIRING_WORKER_STALE).alerts)
    assert text == (
        "🚨 <b>هشدار Proxy-Hub</b>\n\n"
        "🔴 <b>worker پنل از کار افتاده است</b>\n"
        "آخرین ضربان worker ۳ دقیقه و ۲۰ ثانیه پیش بوده است (حد مجاز ۹۰ ثانیه). چرخه‌های کنترل اجرا نمیشوند.\n"
        "⏱ از ساعت ۲۳:۵۴ (تهران)"  # 20:24:26Z = 23:54 in Tehran (UTC+03:30)
    )


def test_two_warnings_in_one_group() -> None:
    text = render_alertmanager_message(hook(FIRING_DISK_TWO).alerts)
    assert text.count("🟠 <b>دیسک سرور پنل نزدیک پر شدن است</b>") == 2
    assert "۹۰٪ از دیسک / پر است" in text and "۸۷٪ از دیسک /data پر است" in text
    assert "⏱ از ساعت ۲۳:۵۳ (تهران)" in text


def test_resolved_messages() -> None:
    text = render_alertmanager_message(hook(RESOLVED_WORKER_STALE).alerts)
    assert text == "✅ <b>برطرف شد (Proxy-Hub)</b>\n\n🟢 <b>worker پنل از کار افتاده است</b> (ساعت ۲۳:۵۴)"
    disks = render_alertmanager_message(hook(RESOLVED_DISK_TWO).alerts)
    assert disks.count("🟢") == 2 and "(ساعت ۲۳:۵۸)" in disks and "🚨" not in disks


def test_mixed_group_lists_firing_first_then_resolved() -> None:
    text = render_alertmanager_message(hook(MIXED_REDIS).alerts)
    assert text.index("🚨 <b>هشدار Proxy-Hub</b>") < text.index("✅ <b>برطرف شد (Proxy-Hub)</b>")
    assert text.count("Redis پنل پاسخ نمیدهد") == 2


def test_annotations_are_escaped() -> None:
    data = json.loads(FIRING_WORKER_STALE)
    data["alerts"][0]["annotations"] = {"summary": "<b>x</b> & y", "description": "<script>z</script>"}
    text = render_alertmanager_message(AlertmanagerWebhook.model_validate(data).alerts)
    assert "<b>&lt;b&gt;x&lt;/b&gt; &amp; y</b>" in text and "&lt;script&gt;z&lt;/script&gt;" in text
    assert "<script>" not in text


def test_many_alerts_fit_one_message_with_a_count_of_the_rest() -> None:
    data = json.loads(FIRING_DISK_TWO)
    template = data["alerts"][0]
    data["alerts"] = [
        {
            **template,
            "fingerprint": f"{i:016x}",
            "annotations": {"summary": "s" * 200, "description": "d" * 900},
        }
        for i in range(40)
    ]
    data["truncatedAlerts"] = 3
    parsed = AlertmanagerWebhook.model_validate(data)
    text = render_alertmanager_message(parsed.alerts, truncated=parsed.truncated_alerts)
    assert len(text) <= MAX_MESSAGE_CHARS and utf16_len(visible_text(text)) <= MAX_MESSAGE_CHARS
    shown = text.count("🟠")
    assert 0 < shown < 40
    from proxyhub.telegram.templates import fa_digits

    assert text.endswith(f"… و {fa_digits(40 - shown + 3)} هشدار دیگر")


def test_payload_validation() -> None:
    data = json.loads(FIRING_WORKER_STALE)
    for broken in (
        {**data, "version": "3"},
        {**data, "alerts": "x"},
        {k: v for k, v in data.items() if k != "status"},
    ):
        with pytest.raises(ValueError):
            AlertmanagerWebhook.model_validate(broken)


# --- dedupe and the credentials cache (pure) -----------------------------------------------------


def test_dedupe_by_fingerprint_and_status_for_five_minutes() -> None:
    d = Deduper()
    t0 = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    firing = hook(FIRING_DISK_TWO).alerts
    assert d.fresh(firing, t0) == firing
    d.mark(firing, t0)
    assert d.fresh(firing, t0 + timedelta(minutes=4, seconds=59)) == []
    resolved = hook(RESOLVED_DISK_TWO).alerts  # same fingerprints, other status
    assert d.fresh(resolved, t0 + timedelta(minutes=1)) == resolved
    assert d.fresh(firing, t0 + timedelta(minutes=5)) == firing
    twice = [*hook(FIRING_WORKER_STALE).alerts, *hook(FIRING_WORKER_STALE).alerts]
    assert len(Deduper().fresh(twice, t0)) == 1


def test_dedupe_memory_is_bounded() -> None:
    d = Deduper(max_entries=10)
    t0 = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    base = hook(FIRING_WORKER_STALE).alerts[0]
    d.mark([base.model_copy(update={"fingerprint": f"{i:016x}"}) for i in range(50)], t0)
    assert len(d._seen) == 10


async def test_target_cache_survives_a_database_outage() -> None:
    cache = TargetCache()
    target = OwnerTarget(token=SecretValue(random_token()), chat_id=42)

    async def ok() -> OwnerTarget:
        return target

    async def db_down() -> OwnerTarget:
        raise ConnectionRefusedError("database down")

    async def unpaired() -> OwnerTarget:
        raise NotConfigured("owner_not_paired")

    with pytest.raises(ConnectionRefusedError):
        await cache.get(db_down)  # nothing cached yet
    assert await cache.get(ok) is target
    assert await cache.get(db_down) is target  # last known bot and chat
    with pytest.raises(NotConfigured):
        await cache.get(unpaired)  # the owner un-paired: forget
    with pytest.raises(ConnectionRefusedError):
        await cache.get(db_down)
    assert "***" in repr(target) and target.token.reveal() not in repr(target)


async def test_send_direct_retries_network_errors_then_gives_up() -> None:
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    target = OwnerTarget(token=SecretValue(random_token()), chat_id=42)
    with pytest.raises(NetworkError) as info:
        await send_direct(closed_port_bot(), target, "x", sleep=no_wait)
    assert info.value.maybe_sent is False  # connection refused: nothing was sent
    assert sleeps == [1.0, 2.0]  # 3 attempts


# --- the route over HTTP -------------------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_telegram_tables(eng)
    ALERT_DEDUPER.clear()
    ALERT_TARGETS.clear()
    yield eng
    await clean_telegram_tables(eng)
    ALERT_DEDUPER.clear()
    ALERT_TARGETS.clear()
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_bot_factory] = closed_port_bot
    try:
        async with lifespan(app):
            # No session cookie and no CSRF header: Alertmanager has neither.
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api:8000") as c:
                yield c
    finally:
        app.dependency_overrides.pop(get_vault, None)
        app.dependency_overrides.pop(get_bot_factory, None)


async def configure(engine: AsyncEngine, vault: Vault, *, owner: bool) -> str:
    token = random_token()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=8123456789, username="proxyhub_test_bot"),
            token=SecretValue(token),
            now=now,
        )
        if owner:
            await store.set_owner_chat(conn, 111222333, now)
    return token


async def post(client: AsyncClient, raw: str) -> Any:
    return await client.post(
        ROUTE, content=raw, headers={"Content-Type": "application/json", "User-Agent": "Alertmanager/0.34.2"}
    )


@pytest.mark.db
@pytest.mark.redis
async def test_nobody_to_send_to_is_409_and_not_deduplicated(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    r = await post(client, FIRING_WORKER_STALE)
    assert r.status_code == 409 and r.json()["error"] == "bot_not_set" and r.json()["message_fa"]
    await configure(engine, vault, owner=False)
    r = await post(client, FIRING_WORKER_STALE)
    assert r.status_code == 409 and r.json()["error"] == "owner_not_paired"


@pytest.mark.db
@pytest.mark.redis
async def test_temporary_failure_is_503_so_alertmanager_retries(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    token = await configure(engine, vault, owner=True)
    with all_logs() as logs:
        first = await post(client, FIRING_DISK_TWO)
        second = await post(client, FIRING_DISK_TWO)
    for r in (first, second):  # not marked as delivered, so the retry is tried again
        assert r.status_code == 503 and r.json()["error"] == "telegram_unavailable"
    assert_no_token(token, first.text, logs.getvalue())
    async with engine.connect() as conn:
        assert await store.get_setting(conn, store.KEY_LAST_ALERT) is None


@pytest.mark.db
@pytest.mark.redis
async def test_revoked_token_is_409_with_real_telegram(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    from proxyhub.api.app import app

    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    app.dependency_overrides[get_bot_factory] = lambda: real_bot
    token = await configure(engine, vault, owner=True)
    with all_logs() as logs:
        r = await post(client, MIXED_REDIS)
    assert r.status_code == 409 and r.json()["error"] == "telegram_rejected"
    assert "bot<redacted>/sendMessage" in logs.getvalue()
    assert_no_token(token, r.text, logs.getvalue())


@pytest.mark.db
@pytest.mark.redis
async def test_duplicates_answer_200_without_sending(client: AsyncClient, engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    ALERT_DEDUPER.mark(hook(RESOLVED_WORKER_STALE).alerts, now)
    r = await post(client, RESOLVED_WORKER_STALE)
    assert r.status_code == 200 and r.json() == {"delivered": 0, "duplicates": 1}


@pytest.mark.db
@pytest.mark.redis
async def test_bad_payloads_are_422(client: AsyncClient) -> None:
    data = json.loads(FIRING_WORKER_STALE)
    for raw in (json.dumps({**data, "version": "3"}), "not json", "[]"):
        r = await post(client, raw)
        assert r.status_code == 422 and r.json()["error"] == "invalid_request"


@pytest.mark.db
@pytest.mark.redis
async def test_route_is_internal_only() -> None:
    from proxyhub.api.app import app

    assert ROUTE not in app.openapi()["paths"]
    am = AmAlert.model_validate(json.loads(FIRING_WORKER_STALE)["alerts"][0])
    assert am.starts_at == datetime(2026, 11, 3, 20, 24, 26, tzinfo=UTC)
