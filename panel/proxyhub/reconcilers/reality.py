"""Reality policy loop (team reality): ends configs that are full, expired or burned, and keeps
fresh configs on every ready node (proxyhub/reality/policy.py), every 30 seconds.

Every change goes to actions_log (actor ``reality``) without the config's UUID: that is the
credential a link carries. The owner hears about what code cannot fix: a node from which the
REALITY target fails its TLS 1.3 + HTTP/2 check gets no new configs (at most one alert a day).
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.fleet.actions import iso, log_action
from proxyhub.reality.policy import create_configs, end_configs
from proxyhub.reality.settings import load_reality_settings, load_state, save_state
from proxyhub.reality.store import all_statuses, live_nodes
from proxyhub.reconcilers.base import Reconciler
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.telegram.templates import fa_digits

log = logging.getLogger(__name__)

ACTOR: Final = "reality"
TARGET_ALERT_REPEAT: Final = timedelta(days=1)


def target_alert_text(nodes: list[str]) -> str:
    names = "، ".join(html.escape(n) for n in nodes[:5]) + (" و …" if len(nodes) > 5 else "")
    return (
        "⚠️ <b>Reality</b>: سایت هدف (target) از "
        f"{fa_digits(len(nodes))} node آزمون TLS 1.3 و HTTP/2 را رد کرد ({names}). "
        "این nodeها کانفیگ تازه‌ی Reality نمیگیرند. در صفحه‌ی Reality سایت هدف دیگری بگذارید."
    )


class RealityReconciler(Reconciler):
    name = "reality"
    interval_seconds = 30.0

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        settings = await load_reality_settings(conn)
        for config_id, status in sorted((await end_configs(conn, now)).items()):
            await log_action(
                conn,
                actor=ACTOR,
                action="reality_config_ended",
                target=f"reality:{config_id}",
                dry_run=False,
                allowed=True,
                reasons=[status],
            )
        if settings.enabled:
            for c in await create_configs(conn, settings, now):
                if c.key_created:
                    await log_action(
                        conn,
                        actor=ACTOR,
                        action="reality_key_created",
                        target=f"slot:{c.slot}" if c.slot is not None else f"node:{c.node_id}",
                        dry_run=False,
                        allowed=True,
                        reasons=["new_scope"],
                    )
                await log_action(
                    conn,
                    actor=ACTOR,
                    action="reality_config_created",
                    target=f"reality:{c.config_id}",
                    dry_run=False,
                    allowed=True,
                    reasons=["fresh_config"],
                    context={
                        "node_id": str(c.node_id),
                        "slot": c.slot,
                        "host": c.host,
                        "expires_at": iso(c.expires_at),
                        "cap_gb": settings.cap_gb,
                    },
                )
        await self._target_alert(conn, now)

    async def _target_alert(self, conn: AsyncConnection, now: datetime) -> None:
        statuses = await all_statuses(conn)
        failing = sorted(
            n.name for n in await live_nodes(conn) if (st := statuses.get(n.id)) and st.target_ok is False
        )
        if not failing:
            return
        state = await load_state(conn)
        last = state.get("target_alert_at")
        if isinstance(last, str) and now - datetime.fromisoformat(last) < TARGET_ALERT_REPEAT:
            return
        await enqueue_owner_alert(conn, target_alert_text(failing))
        await log_action(
            conn,
            actor=ACTOR,
            action="reality_target_failing",
            target=None,
            dry_run=False,
            allowed=True,
            reasons=["target_check_failed"],
            context={"nodes": failing[:20]},
        )
        state["target_alert_at"] = now.isoformat()
        await save_state(conn, state)
