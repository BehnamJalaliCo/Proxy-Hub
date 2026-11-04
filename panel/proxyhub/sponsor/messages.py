"""Owner-facing Persian text of the sponsor module: API errors and the owner alert. No text ever
contains a proxy secret; the bot's reply is shown only after proxyhub/sponsor/replies.py masked it."""

from __future__ import annotations

import html
from typing import Final

ERRORS_FA: Final[dict[str, str]] = {
    "invalid_request": "درخواست ناقص یا نادرست است.",
    "not_found": "پیدا نشد؛ شاید پاک یا بازنشسته شده است. صفحه را تازه کنید.",
    "channel_private": (
        "این لینک خصوصی است. ‎@MTProxybot‎ فقط کانال عمومی را میپذیرد: لینکی مثل ‎https://t.me/name‎ "
        "یا ‎@name‎ بدهید."
    ),
    "channel_format": (
        "لینک کانال عمومی را مثل ‎https://t.me/name‎ یا ‎@name‎ وارد کنید (۴ تا ۳۲ حرف لاتین، رقم یا _)."
    ),
    "channel_is_default": "این همان کانال پیش‌فرض اسپانسر است؛ کمپین برای کانال مشتری است.",
    "campaign_time_format": "زمان شروع و پایان را کامل، با منطقه‌ی زمانی، بفرستید.",
    "campaign_end_before_start": "پایان کمپین باید بعد از شروع آن باشد.",
    "campaign_in_past": "پایان کمپین گذشته است.",
    "campaign_start_in_past": "شروع کمپین گذشته است. زمان شروع را از حالا به بعد بگذارید.",
    "campaign_too_long": "یک کمپین حداکثر یک سال است.",
    "campaign_overlap": (
        "این بازه با کمپین دیگری هم‌پوشانی دارد. هر پروکسی در هر لحظه یک اسپانسر دارد؛ کمپین‌ها میتوانند "
        "پشت سر هم باشند (یکی درست وقتی دیگری تمام میشود شروع شود)."
    ),
    "campaign_closed": "این کمپین تمام یا لغو شده است و دیگر تغییر نمیکند.",
    "campaign_started": "این کمپین شروع شده است؛ فقط لغو آن ممکن است، نه حذف.",
    "price_format": "قیمت را عددی مثبت با حداکثر دو رقم اعشار وارد کنید.",
    "currency_format": "واحد پول را با ۳ تا ۸ حرف لاتین بزرگ وارد کنید (مثلاً EUR یا USDT).",
    "customer_format": "نام مشتری حداکثر ۲۰۰ نویسه است.",
    "lead_format": "زمان زودتر شروع شدن تغییر باید بین ۰ تا ۳۶۰ دقیقه باشد.",
    "registration_retired": "این ثبت بازنشسته است (پروکسی‌اش دیگر وجود ندارد) و ثبت دوباره ندارد.",
}

STATUS: Final[dict[str, int]] = {
    "invalid_request": 422,
    "not_found": 404,
    "campaign_overlap": 409,
    "campaign_closed": 409,
    "campaign_started": 409,
    "registration_retired": 409,
}


def error_message(code: str) -> str:
    return ERRORS_FA.get(code, ERRORS_FA["invalid_request"])


STEP_FA: Final[dict[str, str]] = {
    "sent_newproxy": "پس از فرمان ‎/newproxy",
    "sent_address": "پس از فرستادن نشانی و پورت",
    "sent_secret": "پس از فرستادن secret",
    "sent_myproxies": "پس از فرمان ‎/myproxies",
    "pressed_proxy": "پس از انتخاب پروکسی در فهرست",
    "pressed_promotion": "پس از زدن دکمه‌ی «Set promotion»",
    "sent_channel": "پس از فرستادن لینک کانال",
}

REASON_FA: Final[dict[str, str]] = {
    "unexpected_reply": "پاسخ ربات با آنچه انتظار میرفت فرق داشت.",
    "address_refused": "ربات نشانی و پورت را نپذیرفت.",
    "no_tag_in_reply": "در پاسخ ربات tag ۳۲ رقمی پیدا نشد.",
    "proxy_not_in_list": "این پروکسی در فهرست ‎/myproxies‎ نبود.",
    "proxy_listed_twice": "این پروکسی دو بار در فهرست ‎/myproxies‎ بود.",
    "menu_of_another_proxy": "ربات منوی پروکسی دیگری را نشان داد.",
    "no_set_promotion_button": "دکمه‌ی «Set promotion» پیدا نشد.",
    "several_promotion_buttons": "چند دکمه‌ی «Set promotion» بود.",
    "no_channel_prompt": "ربات لینک کانال نخواست.",
    "channel_refused": "ربات لینک کانال را نپذیرفت.",
    "no_reply": "ربات چند بار پشت سر هم جواب نداد.",
    "mtproxybot_changed": (
        "نام ‎@MTProxybot‎ به حساب دیگری میرسد، نه رباتی که هنگام ورود دیده شد. "
        "برای ایمنی هیچ پیامی فرستاده نشد."
    ),
    "mtproxybot_not_found": "حساب عملیاتی ‎@MTProxybot‎ را پیدا نکرد.",
    "telegram_refused": "تلگرام پیام به ‎@MTProxybot‎ را نپذیرفت (نام خطای تلگرام در رویدادها هست).",
}

HINT_FA: Final[dict[str, str]] = {
    "admin_required": (
        "به نظر میرسد ربات میخواهد حساب دوم ادمین کانال باشد (F12). حساب دوم را با کمترین اجازه ادمین همان "
        "کانال کنید و بعد «ثبت دوباره» را بزنید."
    ),
    "channel_private": "ربات کانال را خصوصی میداند. کانال باید عمومی باشد و نام کاربری داشته باشد.",
    "already_registered": "ربات میگوید این پروکسی از قبل ثبت شده است.",
    "invalid_input": "ربات ورودی را نادرست میداند.",
    "bot_limit": "ربات از محدودیت یا «بعداً» حرف میزند.",
}


def stuck_alert(
    *, address: str, step: str, reason: str, hint: str | None, bot_text: str | None, paused: bool
) -> str:
    """The owner alert (HTML) when a conversation with @MTProxybot stopped on a reply the panel does
    not understand. ``bot_text`` is already masked."""
    lines = [
        "⚠️ <b>ثبت پروکسی در ‎@MTProxybot‎ متوقف شد</b>",
        f"پروکسی: <code>{html.escape(address)}</code>",
        f"کجا: {html.escape(STEP_FA.get(step, step))}",
        f"چرا: {html.escape(REASON_FA.get(reason, reason))}",
    ]
    if hint is not None and hint in HINT_FA:
        lines.append(html.escape(HINT_FA[hint]))
    if bot_text:
        shown = bot_text if len(bot_text) <= 600 else bot_text[:599] + "…"
        lines.append(f"آخرین پاسخ ربات:\n<pre>{html.escape(shown)}</pre>")
    if paused:
        lines.append(
            "چون بیش از یک پروکسی به همین وضع خورده است، پنل تا رسیدگی شما با ‎@MTProxybot‎ برای هیچ پروکسی‌ای "
            "گفتگو نمیکند."
        )
    lines.append("پس از بررسی، در صفحه‌ی «اسپانسر» داشبورد دکمه‌ی «ثبت دوباره» را بزنید.")
    return "\n".join(lines)
