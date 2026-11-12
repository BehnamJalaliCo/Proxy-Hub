"""Owner-facing Persian text of the operator-account API and its owner alert. No message ever
contains an input value (phone, api_hash, code, password)."""

from __future__ import annotations

import html
from typing import Final

ERRORS_FA: Final[dict[str, str]] = {
    "invalid_request": "درخواست ناقص یا نادرست است.",
    # input formats
    "api_id_format": "api_id فقط عدد است: همان «App api_id» در my.telegram.org (مثلاً ۱۲۳۴۵۶۷).",
    "api_hash_format": (
        "api_hash باید دقیقاً ۳۲ نویسه از رقم‌های 0 تا 9 و حرف‌های a تا f باشد: "
        "همان «App api_hash» در my.telegram.org."
    ),
    "phone_format": ("شماره‌ی حساب دوم را کامل، با + و کد کشور وارد کنید (مثلاً با ‎+44‎ یا ‎+98‎ شروع میشود)."),
    "code_format": "کد ورودی را که تلگرام فرستاده وارد کنید: فقط رقم‌ها (معمولاً ۵ رقم).",
    "code_word_format": (
        "این بار کد ورود یک کلمه یا یک عبارت است که با پیامک آمده؛ آن را همان‌طور که در پیامک نوشته شده "
        "وارد کنید (فقط حرف و رقم و فاصله)."
    ),
    "password_format": "رمز دومرحله‌ای حساب دوم را وارد کنید (حداکثر ۲۵۶ نویسه).",
    # Telegram's answers
    "api_id_invalid": (
        "تلگرام این api_id و api_hash را با هم نپذیرفت. هر دو را دوباره از my.telegram.org "
        "(با حساب دوم، بخش API development tools) کپی کنید."
    ),
    "api_id_published": (
        "تلگرام این api_id را بسته است، چون جایی منتشر شده. در my.telegram.org یک برنامه‌ی تازه بسازید."
    ),
    "phone_number_invalid": "تلگرام این شماره را معتبر نمیداند. شماره‌ی حساب دوم را با کد کشور وارد کنید.",
    "phone_number_banned": "این شماره در تلگرام مسدود است و نمیتواند وارد شود.",
    "phone_number_unoccupied": (
        "هیچ حساب تلگرامی با این شماره نیست. حساب دوم باید از قبل با برنامه‌ی رسمی تلگرام ساخته شده باشد."
    ),
    "phone_number_flood": "برای این شماره درخواست کد زیاد بوده است. چند ساعت بعد دوباره امتحان کنید.",
    "flood_wait": "تلگرام خواسته است کمی صبر کنیم. پس از زمان گفته‌شده دوباره امتحان کنید.",
    "password_flood": (
        "رمز دومرحله‌ای چند بار اشتباه وارد شد و تلگرام ورود را موقتاً بسته است. بعداً دوباره امتحان کنید."
    ),
    "phone_code_invalid": "کد ورود درست نیست. کد را در پیام تلگرام (در اپ حساب دوم) دوباره نگاه کنید.",
    "phone_code_expired": "کد ورود منقضی شده است. از اول شروع کنید تا کد تازه بیاید.",
    "password_invalid": "رمز دومرحله‌ای درست نیست.",
    "too_many_attempts": "چند بار پشت سر هم اشتباه وارد شد و این ورود لغو شد. از اول شروع کنید.",
    "send_code_unavailable": ("تلگرام فعلاً راه دیگری برای فرستادن کد ندارد. چند دقیقه بعد از اول شروع کنید."),
    "send_code_payment_required": (
        "تلگرام برای فرستادن کد پیامکی به این شماره پرداخت میخواهد. اپ رسمی تلگرام را روی حساب دوم "
        "باز کنید تا کد در خود اپ بیاید، و دوباره امتحان کنید."
    ),
    "update_app_to_login": (
        "تلگرام ورود این نسخه‌ی پنل را نپذیرفت (UPDATE_APP_TO_LOGIN). به‌روزرسانی پنل لازم است."
    ),
    "telegram_refused": "تلگرام این درخواست را نپذیرفت. نام خطای تلگرام در جزئیات آمده است.",
    "telegram_unavailable": "پاسخ تلگرام به دست نیامد. چند دقیقه‌ی دیگر دوباره امتحان کنید.",
    "logout_unavailable": (
        "تلگرام پاسخ نداد و نشست هنوز باز است. چند دقیقه‌ی دیگر دوباره بزنید، یا در اپ تلگرام حساب دوم "
        "از بخش دستگاه‌ها (Devices) نشست پنل را ببندید."
    ),
    # the panel's own rules
    "mtproxy_connected": (
        "یک حساب عملیاتی همین حالا وصل است. برای ورود حساب دیگر، اول «قطع اتصال» را بزنید."
    ),
    "mtproxy_not_connected": "هیچ حسابی وصل نیست و هیچ ورودی هم در جریان نیست.",
    "mtproxy_no_login": "ورودی در این مرحله در جریان نیست. صفحه را تازه کنید و از اول شروع کنید.",
    "mtproxy_login_expired": "زمان این ورود (۵ دقیقه) تمام شد. از اول شروع کنید تا کد تازه بیاید.",
    "mtproxy_busy": (
        "کار دیگری با این حساب در جریان است (مثلاً بررسی خودکار نشست). چند ثانیه‌ی دیگر دوباره امتحان کنید."
    ),
    "vault_unavailable": "کلید رمزنگاری پنل در دسترس نیست؛ کاری انجام نشد.",
    "state_unavailable": "Redis پنل در دسترس نیست و ورود نمیتواند ادامه یابد. کمی بعد دوباره امتحان کنید.",
}

STATUS: Final[dict[str, int]] = {
    "invalid_request": 422,
    "phone_number_flood": 429,
    "flood_wait": 429,
    "password_flood": 429,
    "telegram_unavailable": 502,
    "logout_unavailable": 502,
    "mtproxy_connected": 409,
    "mtproxy_not_connected": 409,
    "mtproxy_no_login": 409,
    "mtproxy_login_expired": 409,
    "mtproxy_busy": 409,
    "vault_unavailable": 503,
    "state_unavailable": 503,
}


def fa_digits(value: object) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def wait_text(seconds: int) -> str:
    """42 -> "۴۲ ثانیه"; 300 -> "۵ دقیقه"; 7300 -> "حدود ۳ ساعت" (rounded up)."""
    if seconds < 60:
        return f"{fa_digits(seconds)} ثانیه"
    if seconds < 3600:
        return f"{fa_digits(-(-seconds // 60))} دقیقه"
    return f"حدود {fa_digits(-(-seconds // 3600))} ساعت"


def error_message(code: str, *, seconds: int | None = None) -> str:
    base = ERRORS_FA.get(code, ERRORS_FA["telegram_refused"])
    if code == "flood_wait" and seconds is not None and seconds > 0:
        return f"تلگرام خواسته است {wait_text(seconds)} صبر کنیم. پس از آن دوباره امتحان کنید."
    return base


def session_ended_alert(reason: str) -> str:
    """The owner alert (HTML) when Telegram ended the operator account's session."""
    return (
        "⚠️ <b>نشست حساب عملیاتی ‎@MTProxybot‎ بسته شد</b>\n"
        f"تلگرام میگوید نشست پنل دیگر معتبر نیست (<code>{html.escape(reason)}</code>): شاید در اپ تلگرام از "
        "بخش دستگاه‌ها بسته شده، یا حساب دوم حذف یا مسدود شده است.\n"
        "تا ورود دوباره، پنل به این حساب دسترسی ندارد. برای وصل کردن دوباره، از صفحه‌ی «تلگرام» داشبورد "
        "دوباره وارد شوید."
    )


# Check failures that will not pass by themselves: a new login, for the api_id ones with a new app.
_NEEDS_NEW_LOGIN: Final = frozenset({"session_unreadable", "session_missing"})
_NEEDS_NEW_API_ID: Final = frozenset({"api_id_invalid", "api_id_published"})


def check_failed_alert(code: str, rpc: str | None) -> str:
    """The owner alert (HTML) when the session check fails for a reason that does not pass at once
    (the account moved to ``error``). Sent once per cause; the check keeps running every 30 minutes."""
    shown = html.escape(f"{code} / {rpc}" if rpc else code)
    if code in _NEEDS_NEW_API_ID:
        todo = (
            "این معمولاً خودبه‌خود درست نمیشود: در my.telegram.org یک برنامه‌ی تازه بسازید، در صفحه‌ی "
            "«تلگرام» داشبورد «قطع اتصال» را بزنید و با api_id تازه دوباره وارد شوید."
        )
    elif code in _NEEDS_NEW_LOGIN:
        todo = (
            "اگر بررسی بعدی هم درست نشد، در صفحه‌ی «تلگرام» داشبورد «قطع اتصال» را بزنید و دوباره وارد شوید."
        )
    else:
        todo = (
            "اگر چند بررسی پشت سر هم درست نشد، در صفحه‌ی «تلگرام» داشبورد «قطع اتصال» را بزنید و "
            "دوباره وارد شوید."
        )
    return (
        "⚠️ <b>حساب عملیاتی ‎@MTProxybot‎ فعلاً کار نمیکند</b>\n"
        f"بررسی خودکار نشست ناموفق بود (<code>{shown}</code>). تا درست شود، ثبت اسپانسر، پست با حساب دوم، "
        "آمار کانال و خواندن منبع‌ها با این حساب متوقف است.\n"
        f"پنل هر {fa_digits(30)} دقیقه دوباره بررسی میکند و اگر علت برطرف شود، حساب خودش دوباره وصل میشود. "
        f"{todo}"
    )


def check_recovered_alert() -> str:
    """The owner alert (HTML) when a check passes again after the account was in ``error``."""
    return "✅ <b>حساب عملیاتی ‎@MTProxybot‎ دوباره کار میکند</b>\nبررسی خودکار نشست موفق بود؛ کاری لازم نیست."
