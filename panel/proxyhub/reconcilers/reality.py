"""Reality policy loop (team reality), every 30 seconds (proxyhub/reality/policy.py): ends configs
that are full, expired or burned; moves key scopes to the owner's target setting; makes keys for new
scopes and fresh configs on nodes that confirmed their scope's target; deletes the vaulted key of a
slotless node that is gone.

Every change goes to actions_log (actor ``reality``) without the config's UUID: that is the
credential a link carries. The owner hears about what code cannot fix by itself: a node from which
the REALITY target fails its TLS 1.3 + HTTP/2 check (at most one alert a day).
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.db.models_reality import RealityKey
from proxyhub.fleet.actions import iso, log_action
from proxyhub.reality.policy import (
    create_configs,
    end_configs,
    ensure_keys,
    forget_gone_node_keys,
    renew_configs,
    retarget_scopes,
    scope_label,
)
from proxyhub.reality.settings import load_reality_settings, load_state, save_state
from proxyhub.reality.store import all_statuses, live_nodes, target_verdict
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
        "روی این nodeها کانفیگ تازه‌ی Reality ساخته نمیشود. node هر ۶ ساعت دوباره میآزماید و اگر "
        "درست شد، ساخت کانفیگ خودش از سر گرفته میشود.\n"
        "اگر در صفحه‌ی Reality سایت هدف دیگری بگذارید، کانفیگ‌های فعلی این nodeها (که با این هدف "
        "کار نمیکنند) کنار میروند، node سایت تازه را میآزماید و اگر قبول شد، کانفیگ تازه با آن "
        "ساخته میشود."
    )


class RealityReconciler(Reconciler):
    name = "reality"
    interval_seconds = 30.0

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        settings = await load_reality_settings(conn)
        for config_id in await renew_configs(conn, settings, now):  # D41: before anything ends
            await log_action(
                conn,
                actor=ACTOR,
                action="reality_config_renewed",
                target=f"reality:{config_id}",
                dry_run=False,
                allowed=True,
                reasons=["still_working"],
                context={"hours": settings.cap_hours, "gb": settings.cap_gb},
            )
        for config_id, status in sorted((await end_configs(conn, now)).items()):
            await self._ended(conn, config_id, [status])
        for node_id in await forget_gone_node_keys(conn):
            await log_action(
                conn,
                actor=ACTOR,
                action="reality_key_forgotten",
                target=scope_label(None, node_id),
                dry_run=False,
                allowed=True,
                reasons=["node_deleted"],
            )
        for r in await retarget_scopes(conn, settings, now):
            for config_id in r.burned:
                await self._ended(conn, config_id, ["burned", "target_replaced"])
            await log_action(
                conn,
                actor=ACTOR,
                action="reality_target_switched",
                target=scope_label(r.slot, r.node_id),
                dry_run=False,
                allowed=True,
                reasons=["old_target_failed" if r.burned else "no_active_configs"],
                context={"from": r.old_target, "to": r.new_target, "burned": len(r.burned)},
            )
        if settings.enabled:
            for k in await ensure_keys(conn, settings, now):
                await log_action(
                    conn,
                    actor=ACTOR,
                    action="reality_key_created",
                    target=scope_label(k.slot, k.node_id),
                    dry_run=False,
                    allowed=True,
                    reasons=["new_scope"],
                    context={"target": settings.target},
                )
            for c in await create_configs(conn, settings, now):
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

    async def _ended(self, conn: AsyncConnection, config_id: int, reasons: list[str]) -> None:
        await log_action(
            conn,
            actor=ACTOR,
            action="reality_config_ended",
            target=f"reality:{config_id}",
            dry_run=False,
            allowed=True,
            reasons=reasons,
        )

    async def _target_alert(self, conn: AsyncConnection, now: datetime) -> None:
        """Nodes whose check of their scope's current target failed (a result about a target the
        owner has replaced does not count)."""
        statuses = await all_statuses(conn)
        targets = {
            (("slot", k.slot) if k.slot is not None else ("node", k.node_id)): (k.target, k.server_name)
            for k in (
                await conn.execute(
                    select(RealityKey.slot, RealityKey.node_id, RealityKey.target, RealityKey.server_name)
                )
            ).all()
        }
        failing: list[str] = []
        for n in await live_nodes(conn):
            target = targets.get(("slot", n.slot) if n.slot is not None else ("node", n.id))
            if target is not None and target_verdict(statuses.get(n.id), *target) is False:
                failing.append(n.name)
        if not failing:
            return
        failing.sort()
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
