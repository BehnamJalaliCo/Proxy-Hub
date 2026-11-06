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
    "mtproxybot_job_running": (
        "یک کار دیگر با فهرست ‎@MTProxybot‎ در جریان است. صبر کنید تمام شود، یا اول «توقف» را بزنید."
    ),
    "mtproxybot_no_job": "کاری با فهرست ‎@MTProxybot‎ در جریان نیست.",
    "mtproxybot_kill_switch": "کلید توقف روشن است؛ تا خاموش نشود پنل با ‎@MTProxybot‎ گفتگو نمیکند.",
    "mtproxybot_no_account": "حساب عملیاتی (حساب دوم تلگرام) وصل نیست. اول آن را در صفحه‌ی تلگرام وصل کنید.",
    "mtproxybot_not_observed": (
        "حذف واقعی فقط پس از یک اجرای «مشاهده» روشن میشود که صفحه‌ی یک پروکسی مرده را دیده باشد و دقیقاً "
        "یک دکمه‌ی حذف با شکل شناخته‌شده در آن بوده باشد."
    ),
}

STATUS: Final[dict[str, int]] = {
    "invalid_request": 422,
    "not_found": 404,
    "campaign_overlap": 409,
    "campaign_closed": 409,
    "campaign_started": 409,
    "registration_retired": 409,
    "mtproxybot_job_running": 409,
    "mtproxybot_no_job": 409,
    "mtproxybot_kill_switch": 409,
    "mtproxybot_no_account": 409,
    "mtproxybot_not_observed": 409,
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
    "pressed_next_page": "پس از رفتن به صفحه‌ی بعد فهرست",
    "pressed_remove": "پس از زدن دکمه‌ی حذف",
    "pressed_confirm": "پس از تأیید حذف",
}

REASON_FA: Final[dict[str, str]] = {
    "unexpected_reply": "پاسخ ربات با آنچه انتظار میرفت فرق داشت.",
    "address_refused": "ربات نشانی و پورت را نپذیرفت.",
    "no_tag_in_reply": "در پاسخ ربات tag ۳۲ رقمی پیدا نشد.",
    "proxy_not_in_list": "این پروکسی در فهرست ‎/myproxies‎ نبود.",
    "proxy_listed_twice": "این پروکسی دو بار در فهرست ‎/myproxies‎ بود.",
    "tag_not_in_list": (
        "نشانی این پروکسی در فهرست ‎/myproxies‎ بود، ولی هیچ دکمه‌ای tag ثبت ما را نداشت "
        "(احتمالاً ثبت قدیمی همین IP است)."
    ),
    "menu_of_another_tag": "صفحه‌ی پروکسی tag دیگری داشت، نه tag ثبت ما؛ چیزی زده نشد.",
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


# --- @MTProxybot's own list: inventory and cleanup (proxyhub/sponsor/inventory.py) ------------------------

LIST_REASON_FA: Final[dict[str, str]] = {
    "unexpected_reply": "پاسخ ‎/myproxies‎ فهرست همیشگی ربات نبود.",
    "wrong_step": "ربات در جای نادرستی از گفتگو جواب داد.",
    "proxy_not_in_list": "این پروکسی در فهرست ربات پیدا نشد.",
    "proxy_listed_twice": "دو دکمه‌ی یکسان برای این پروکسی بود و معلوم نبود کدام است.",
    "menu_of_another_proxy": "ربات صفحه‌ی پروکسی دیگری را نشان داد.",
    "menu_does_not_name_proxy": "صفحه‌ی پروکسی نشانی خودش را نشان نداد؛ برای ایمنی چیزی زده نشد.",
    "menu_of_live_proxy": "صفحه‌ی پروکسی tag یک پروکسی زنده‌ی ما را داشت؛ حذف زده نشد.",
    "no_remove_button": "در صفحه‌ی پروکسی دکمه‌ای با شکل شناخته‌شده‌ی حذف نبود.",
    "several_remove_buttons": "در صفحه‌ی پروکسی چند دکمه‌ی حذف بود؛ برای ایمنی چیزی زده نشد.",
    "confirm_of_another_proxy": "صفحه‌ی تأیید نام پروکسی دیگری را داشت؛ تأیید زده نشد.",
    "confirm_does_not_name_proxy": "صفحه‌ی تأیید نشانی همین پروکسی را نشان نداد؛ تأیید زده نشد.",
    "no_confirm_button": "در صفحه‌ی تأیید دکمه‌ای با شکل شناخته‌شده‌ی تأیید نبود؛ تأیید زده نشد.",
    "several_confirm_buttons": "در صفحه‌ی تأیید چند دکمه‌ی تأیید بود؛ برای ایمنی چیزی زده نشد.",
    "removal_not_confirmed": "پس از تأیید، ربات نگفت که پروکسی حذف شد. فهرست بعدی نشان میدهد حذف شده یا نه.",
    "interrupted": "گفتگو پس از زدن دکمه‌ی حذف قطع شد؛ پنل آن را خودکار ادامه نمیدهد.",
    "list_incomplete": "همه‌ی صفحه‌های فهرست خوانده نشد؛ بدون فهرست کامل هیچ چیزی حذف نمیشود.",
    "next_page_did_not_change": "دکمه‌ی صفحه‌ی بعد، فهرست را عوض نکرد.",
    "several_next_buttons": "چند دکمه‌ی صفحه‌ی بعد بود؛ فقط صفحه‌ی اول خوانده شد.",
    "too_many_pages": "فهرست بیش از ۸ صفحه بود؛ بقیه خوانده نشد.",
    "nothing_to_remove": "پروکسی مرده‌ای از ما در فهرست ربات نبود.",
    "owner": "به درخواست شما متوقف شد.",
    "removal_disabled": "حذف واقعی هنوز روشن نشده است.",
    "no_reply": "‎@MTProxybot‎ چند بار جواب نداد.",
    "too_many_steps": "گفتگو بیش از اندازه طول کشید.",
    "telegram_unavailable": "به تلگرام نرسید.",
    "flood_wait": "تلگرام خواست صبر کنیم.",
    "mtproxybot_changed": "نام ‎@MTProxybot‎ به حساب دیگری میرسد؛ برای ایمنی پیامی فرستاده نشد.",
    "telegram_refused": "تلگرام درخواست را نپذیرفت.",
    "request_not_allowed": "فهرست مجاز حساب دوم این دکمه را نپذیرفت؛ چیزی فرستاده نشد.",
}


def list_reason(code: str | None) -> str | None:
    if code is None:
        return None
    return LIST_REASON_FA.get(code.split(":", 1)[0], code)


def cleanup_stuck_alert(*, address: str, step: str | None, reason: str) -> str:
    """The owner alert (HTML) when removing a dead proxy from @MTProxybot stopped on a screen the
    panel does not know. Nothing more is pressed until the owner starts the cleanup again."""
    lines = [
        "⚠️ <b>پاک کردن پروکسی‌های مرده از ‎@MTProxybot‎ متوقف شد</b>",
        f"پروکسی: <code>{html.escape(address)}</code>",
    ]
    if step:
        lines.append(f"کجا: {html.escape(STEP_FA.get(step, step))}")
    lines.append(f"چرا: {html.escape(list_reason(reason) or reason)}")
    lines.append("پنل چیز دیگری نمیزند. شکل صفحه‌ها (بی secret) در صفحه‌ی «اسپانسر» داشبورد ثبت شده است.")
    return "\n".join(lines)
