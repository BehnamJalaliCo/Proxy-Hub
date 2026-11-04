"""The dashboard's view of Reality: settings, per-node Xray state, configs with what is left.

Links (with their UUID) appear here for active configs only, because the owner publishes them on
purpose (rule 3's one exception); ended configs show no link. Nothing here is per person.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_reality import RealityConfig
from proxyhub.fleet.actions import iso
from proxyhub.reality.links import ENDED_KEPT_FOR, config_name, link_prefix, links_for, vless_url
from proxyhub.reality.settings import GB, PORT, RealitySettings, load_reality_settings
from proxyhub.reality.store import (
    all_statuses,
    keys_by_scope,
    live_nodes,
    node_scope,
    recent_and_active_configs,
    target_verdict,
)

Db = AsyncConnection | AsyncSession


def settings_json(s: RealitySettings) -> dict[str, Any]:
    return {
        **s.model_dump(mode="json"),
        "port": PORT,
        "gb_bytes": GB,
    }


async def summary(db: Db, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = await load_reality_settings(db)
    statuses = await all_statuses(db)
    nodes = await live_nodes(db)
    rows = await recent_and_active_configs(db, now - ENDED_KEPT_FOR)
    listed = {link.key for link, _ in await links_for(db, now)}
    prefix = await link_prefix(db)
    active_by_key: dict[int, int] = {}
    for r in rows:
        if r.status == "active":
            active_by_key[r.key_id] = active_by_key.get(r.key_id, 0) + 1

    keys = await keys_by_scope(db)
    node_items: list[dict[str, Any]] = []
    for n in sorted(nodes, key=lambda n: (n.slot is None, n.slot or 0, n.name)):
        st = statuses.get(n.id)
        key = keys.get(node_scope(n))
        node_items.append(
            {
                "node_id": str(n.id),
                "name": n.name,
                "slot": n.slot,
                "location": n.location,
                "state": n.state,
                "xray": None
                if st is None
                else {
                    "enabled": st.enabled,
                    "up": st.up,
                    "version": st.version,
                    "uptime_s": st.uptime_s,
                    # Only a check of the scope's current target counts (null: not checked yet).
                    "target_ok": target_verdict(st, *key.target_pair) if key else None,
                    "apply_error": st.apply_error,
                    "reported_at": iso(st.reported_at),
                },
                "public_key": key.public_key if key else None,
                "target": key.target if key else None,
                # The owner's newer target, while this scope still waits for its configs to end.
                "target_next": settings.target
                if key and key.target_pair != (settings.target, settings.target_host)
                else None,
                "active_configs": active_by_key.get(key.id, 0) if key else 0,
            }
        )

    config_items: list[dict[str, Any]] = []
    for r in rows:
        active = r.status == "active"
        link_key = f"reality:{r.id}"
        config_items.append(
            {
                "id": r.id,
                "key": link_key,
                "name": config_name(prefix, r.location, r.id),
                "slot": r.slot,
                "node_name": r.node_name,
                "location": r.location,
                "host": r.host,
                "status": r.status,
                "listed": link_key in listed,
                "created_at": iso(r.created_at),
                "expires_at": iso(r.expires_at),
                "ended_at": iso(r.ended_at),
                "remaining_s": max(0, int((r.expires_at - now).total_seconds())) if active else 0,
                "data_cap_bytes": r.data_cap_bytes,
                "used_bytes": r.used_bytes,
                "remaining_bytes": max(0, r.data_cap_bytes - r.used_bytes) if active else 0,
                "url": vless_url(
                    user_id=r.uuid,
                    host=r.host,
                    server_name=r.server_name,
                    public_key=r.public_key,
                    short_id=r.short_id,
                    name=config_name(prefix, r.location, r.id),
                )
                if active
                else None,
            }
        )
    counts = dict(
        (await db.execute(select(RealityConfig.status, func.count()).group_by(RealityConfig.status))).all()
    )
    total_used = (await db.execute(select(func.coalesce(func.sum(RealityConfig.used_bytes), 0)))).scalar_one()
    return {
        "settings": settings_json(settings),
        "defaults": settings_json(RealitySettings()),
        "totals": {
            "active": int(counts.get("active", 0)),
            "expired": int(counts.get("expired", 0)),
            "full": int(counts.get("full", 0)),
            "burned": int(counts.get("burned", 0)),
            "used_bytes": int(total_used),
            "nodes_enabled": sum(1 for s in statuses.values() if s.enabled),
            "nodes_up": sum(1 for s in statuses.values() if s.enabled and s.up),
        },
        "nodes": node_items,
        "configs": config_items,
        "generated_at": iso(now),
    }
