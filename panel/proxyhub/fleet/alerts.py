"""Owner alerts of the live fleet (Persian, through the panel bot's private chat).

Decision D1: the owner is told only what the code cannot solve by itself. Here that is a NEEDED
action the safety gates block (a slot needs a server, but the budget cap, the node cap, the
account's server limit ... say no), a server the delete gate will not delete, and Hetzner handing
out burned IPs again and again.

Each kind is one row in ``fleet_alerts``: an alert goes out when the condition has lasted a short
while (transient blocks stay quiet), is repeated at most every 12 hours while it lasts, and once it
cleared, a new occurrence is told again (at most hourly). "Every allowed location is closed from
Iran" is told at most once a day (:data:`DAILY`). Every alert is also in ``actions_log``.
Nothing secret is ever part of an alert.
"""

from __future__ import annotations

import html
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_fleet import FleetAlert
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.costs import money
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.fleet.planner import FleetPlan, FleetSnapshot
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.telegram.templates import fa_digits

Db = AsyncConnection | AsyncSession
ACTOR = "fleet"

REPEAT = timedelta(hours=12)
RENOTIFY_GAP = timedelta(hours=1)
CLEAR_AFTER = timedelta(minutes=10)
BURNED_IP_WINDOW = timedelta(hours=6)
BURNED_IP_STREAK = 3

# Gate/planner reasons that block a NEEDED creation and that only the owner can lift, with how
# long the block must last before the owner is told.
CREATE_BLOCKS: dict[str, timedelta] = {
    "budget_cap_reached": timedelta(minutes=5),
    "node_cap_reached": timedelta(minutes=5),
    "budget_cap_not_set": timedelta(0),
    "node_cap_not_set": timedelta(0),
    "account_server_limit_reached": timedelta(minutes=5),
    "no_active_account": timedelta(minutes=5),
    "account_guard_failed": timedelta(minutes=30),
    "account_not_usable": timedelta(minutes=30),
    "no_live_price": timedelta(minutes=30),
    "no_other_location": timedelta(minutes=30),
    "spare_in_use": timedelta(hours=1),
    "agent_domain_not_set": timedelta(minutes=5),
    "all_locations_closed": timedelta(minutes=5),
}
# Told at most once a day, also after the condition cleared and came back (P15: the owner asked
# for a daily note when every allowed location is closed from Iran).
DAILY = frozenset({"all_locations_closed"})
DAY = timedelta(days=1)
# can_delete refusals the code cannot fix (server_not_observed and the kill switch clear by themselves).
DELETE_BLOCKS = frozenset(
    {
        "label_managed_by_missing",
        "role_not_deletable",
        "delete_protection_on",
        "db_row_hetzner_id_mismatch",
        "db_row_account_mismatch",
        "account_not_usable",
    }
)
DELAY: dict[str, timedelta] = {
    **CREATE_BLOCKS,
    "delete_blocked": timedelta(minutes=30),
    "burned_ip_streak": timedelta(0),
}


@dataclass(frozen=True)
class Finding:
    code: str
    text: str
    detail: Mapping[str, Any]


def _slots_fa(slots: list[int]) -> str:
    nums = "، ".join(fa_digits(n) for n in sorted(set(slots)))
    return f"جایگاه {nums}" if len(set(slots)) == 1 else f"جایگاه‌های {nums}"


def _create_text(code: str, slots: list[int], plan: FleetPlan, snap: FleetSnapshot) -> str:
    need = f"⚠️ ناوگان: {_slots_fa(slots)} به سرور تازه نیاز دارد، ولی "
    s = snap.settings
    cap = s.budget_cap_eur
    texts = {
        "budget_cap_reached": (
            f"سقف بودجه‌ی ماه ({fa_digits(money(cap or Decimal(0)))} یورو) اجازه نمیدهد. "
            f"خرج پیش‌بینی‌شده‌ی این ماه {fa_digits(money(plan.projected_month_eur))} یورو است. "
            "اگر میخواهید ادامه دهد، سقف بودجه را در «تنظیمات» داشبورد بالا ببرید."
        ),
        "node_cap_reached": (
            f"سقف تعداد node ({fa_digits(s.node_cap_total or 0)}) پر است. "
            "اگر میخواهید، سقف را در «تنظیمات» داشبورد بالا ببرید."
        ),
        "budget_cap_not_set": (
            "سقف بودجه‌ی ماه در «تنظیمات» داشبورد تعیین نشده است؛ بی آن هیچ سروری ساخته نمیشود."
        ),
        "node_cap_not_set": (
            "سقف تعداد node در «تنظیمات» داشبورد تعیین نشده است؛ بی آن هیچ سروری ساخته نمیشود."
        ),
        "account_server_limit_reached": (
            "حساب هتزنر به سقف تعداد سرورش رسیده است. یک حساب هتزنر تازه در صفحه‌ی «حساب‌های هتزنر» "
            "اضافه کنید، یا از هتزنر افزایش سقف بخواهید."
        ),
        "no_active_account": (
            "هیچ حساب هتزنر فعالی نیست. یک حساب تازه در صفحه‌ی «حساب‌های هتزنر» داشبورد اضافه کنید."
        ),
        "account_guard_failed": (
            "نگهبان حساب هتزنر سبز نیست: در project سروری هست که مال این سیستم نیست، یا وارسی حساب "
            "ناموفق است. صفحه‌ی «حساب‌های هتزنر» را ببینید."
        ),
        "account_not_usable": "حساب هتزنر در وضعیتی نیست که در آن سرور ساخته شود.",
        "no_live_price": (
            "برای نوع سرور مجاز در locationهای تنظیمات، قیمت زنده یا موجودی در هتزنر نیست. "
            "نوع سرور یا location دیگری را در «تنظیمات» مجاز کنید."
        ),
        "no_other_location": (
            "location دیگری برای سرور جایگزین نمانده است. location دیگری در «تنظیمات» اضافه کنید."
        ),
        "agent_domain_not_set": (
            "دامنه‌ی سایت عامل‌ها (AGENT_DOMAIN) روی سرور پنل تنظیم نشده است؛ بی آن هیچ node تازه‌ای "
            "نمیتواند در پنل ثبت شود. پرامپت استقرار پنل را دوباره اجرا کنید."
        ),
        "all_locations_closed": (
            "همه‌ی locationهای مجاز در «تنظیمات» ("
            + "، ".join(html.escape(loc) for loc in s.locations)
            + ") در ۷ روز گذشته از همراه اول و رایتل بسته بوده‌اند (دست‌دادن TLS از ایران ناموفق). "
            "سرور تازه در آن‌ها ساخته نمیشود، چون از ایران باز نمیشود. location دیگری را در «تنظیمات» "
            "اضافه کنید؛ جدول «دسترسی locationها از ایران» در صفحه‌ی nodeها نشان میدهد کدام باز بوده است."
        ),
        "spare_in_use": (
            "چند جایگاه هم‌زمان سرور جایگزین میخواهند و جای خالی (سرور دهم) بیش از یک ساعت است در کار است. "
            "ناوگان یکی‌یکی جایگزین میکند؛ اگر طول کشید، سقف تعداد node را یکی بالا ببرید."
        ),
    }
    return need + texts[code]


def findings(plan: FleetPlan, snap: FleetSnapshot) -> list[Finding]:
    """What the owner must hear about in this plan (pure)."""
    blocked: dict[str, list[int]] = defaultdict(list)
    for a in plan.actions:
        if a.action != "create" or a.allowed:
            continue
        slot = int(a.context.get("slot") or 0)
        for r in a.reasons:
            if r in CREATE_BLOCKS:
                blocked[r].append(slot)
    out = [
        Finding(code, _create_text(code, slots, plan, snap), {"slots": sorted(set(slots))})
        for code, slots in sorted(blocked.items())
    ]
    stuck = [
        (a.target, [r for r in a.reasons if r in DELETE_BLOCKS])
        for a in plan.actions
        if a.action == "delete" and not a.allowed and any(r in DELETE_BLOCKS for r in a.reasons)
    ]
    if stuck:
        lines = "\n".join(
            f"• <code>{html.escape(name)}</code>: {html.escape(', '.join(rs))}" for name, rs in stuck
        )
        out.append(
            Finding(
                "delete_blocked",
                "⚠️ ناوگان: این سرورها باید حذف شوند، ولی قفل حذف آن‌ها را رد میکند (تا حل شود هزینه دارند):\n"
                + lines,
                {"nodes": [name for name, _ in stuck]},
            )
        )
    since = snap.now - BURNED_IP_WINDOW
    burned_ip = [
        n
        for n in snap.nodes
        if n.retire_reason == "burned_ip"
        and n.state in (NodeState.RETIRING, NodeState.DELETED)
        and n.created_at >= since
    ]
    if len(burned_ip) >= BURNED_IP_STREAK:
        out.append(
            Finding(
                "burned_ip_streak",
                f"⚠️ ناوگان: در ۶ ساعت گذشته {fa_digits(len(burned_ip))} سرور تازه IP سوخته (یا IP از رنج "
                "سوخته) گرفتند و همان دم حذف شدند. ناوگان در locationهای دیگر دوباره میسازد؛ اگر ادامه یافت، "
                "شاید وقت افزودن یک حساب هتزنر تازه باشد.",
                {"count": len(burned_ip)},
            )
        )
    return out


async def raise_alerts(db: Db, found: list[Finding], now: datetime) -> list[str]:
    """Record the findings, tell the owner when due. Returns the codes sent now."""
    table = FleetAlert.__table__
    rows = {r.code: r for r in (await db.execute(select(table).with_for_update())).all()}
    sent: list[str] = []
    present = {f.code for f in found}
    for f in found:
        row = rows.get(f.code)
        if row is None:
            await db.execute(
                insert(FleetAlert).values(
                    code=f.code,
                    active=True,
                    since=now,
                    last_seen_at=now,
                    last_sent_at=None,
                    detail=dict(f.detail),
                )
            )
            since, last_sent = now, None
        else:
            since = row.since if row.active else now
            last_sent = row.last_sent_at
            await db.execute(
                update(FleetAlert)
                .where(FleetAlert.code == f.code)
                .values(active=True, since=since, last_seen_at=now, detail=dict(f.detail))
            )
        if now - since < DELAY.get(f.code, timedelta(0)):
            continue
        if last_sent is None:
            due = True
        elif f.code in DAILY:
            due = now - last_sent >= DAY
        elif last_sent < since:  # a new streak after the condition had cleared
            due = now - last_sent >= RENOTIFY_GAP
        else:
            due = now - last_sent >= REPEAT
        if not due:
            continue
        await db.execute(update(FleetAlert).where(FleetAlert.code == f.code).values(last_sent_at=now))
        await enqueue_owner_alert(db, f.text)
        await log_action(
            db,
            actor=ACTOR,
            action="fleet_alert",
            target=f.code,
            dry_run=False,
            allowed=False,
            reasons=[f.code],
            context={"since": iso(since), **dict(f.detail)},
            result="the owner was alerted: the fleet cannot do this by itself",
        )
        sent.append(f.code)
    for code, row in rows.items():
        if code not in present and row.active and now - row.last_seen_at >= CLEAR_AFTER:
            await db.execute(update(FleetAlert).where(FleetAlert.code == code).values(active=False))
    return sent
