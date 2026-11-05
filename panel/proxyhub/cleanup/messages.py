"""Owner-facing Persian text of the two cleaning buttons (errors, and why a cleaning stopped early)."""

from __future__ import annotations

from typing import Final

ERRORS_FA: Final[dict[str, str]] = {
    "invalid_min_age": "کمینه‌ی عمر باید عددی بین ۱ تا ۷۲۰ ساعت باشد.",
    "cleanup_busy": (
        "پنل همین حالا روی همین داده‌ها کار میکند و چیزی پاک نشد. یکی دو دقیقه‌ی دیگر دوباره امتحان کنید."
    ),
    "bot_not_set": "هنوز توکن ربات تلگرام وارد نشده است.",
    "test_channel_not_set": (
        "کانال آزمایشی ثبت نشده است. این دکمه فقط کانال آزمایشی را پاک میکند و هرگز به کانال اصلی دست نمیزند."
    ),
    "test_is_main": (
        "کانال آزمایشی و کانال اصلی یکی ثبت شده‌اند. برای حفظ کانال اصلی هیچ پیامی پاک نشد. "
        "کانال آزمایشی را در صفحه‌ی تلگرام درست ثبت کنید."
    ),
    "bot_not_admin": "ربات در کانال آزمایشی ادمین نیست. ربات را دوباره ادمین کانال آزمایشی کنید.",
    "bot_cannot_delete": (
        "ربات در کانال آزمایشی اجازه‌ی «حذف پیام‌ها» ندارد. در تلگرام، در تنظیمات ادمین‌های کانال آزمایشی، "
        "این اجازه را موقتاً به ربات بدهید و دوباره امتحان کنید. پس از پاک شدن کانال، آن را دوباره خاموش کنید."
    ),
    "send_pending": (
        "یک پست در راه کانال آزمایشی است. چند دقیقه صبر کنید تا فرستاده شود، بعد دوباره امتحان کنید."
    ),
    "cleanup_running": "پاک کردن کانال آزمایشی همین حالا در جریان است. چند دقیقه صبر کنید.",
    "confirm_required": "برای تأیید، عبارت «پاک کن» را دقیقاً بنویسید.",
    "channel_not_found": (
        "تلگرام کانال آزمایشی را پیدا نکرد. ربات را ادمین کانال آزمایشی کنید "
        "و در صفحه‌ی تلگرام «وارسی دوباره» را بزنید."
    ),
    "channels_changed": "کانال‌ها همین حالا عوض شدند و چیزی پاک نشد. دوباره امتحان کنید.",
    "bot_token_invalid": (
        "توکن ربات درست نیست یا در BotFather باطل شده است. توکن تازه را در صفحه‌ی تلگرام وارد کنید."
    ),
    "telegram_unavailable": "پاسخ تلگرام به دست نیامد. چند دقیقه‌ی دیگر دوباره امتحان کنید.",
    "vault_unavailable": "کلید رمزنگاری پنل در دسترس نیست؛ کار انجام نشد.",
}

# Why a test-channel cleaning stopped before the end (proxyhub/cleanup/test_channel.py). The part
# already done stays done; pressing the button again goes on (Telegram skips removed ids).
STOPS_FA: Final[dict[str, str]] = {
    "budget": "وقت این دور تمام شد و بخشی از پیام‌ها مانده است. دوباره «پاک کردن کانال آزمایشی» را بزنید.",
    "flood_wait": "تلگرام خواست مدتی صبر کنیم. چند دقیقه‌ی دیگر دوباره دکمه را بزنید.",
    "bot_cannot_delete": "اجازه‌ی «حذف پیام‌ها» در میانه‌ی کار از ربات گرفته شد. بخشی از پیام‌ها مانده است.",
    "bot_token_invalid": ERRORS_FA["bot_token_invalid"],
    "channel_not_found": ERRORS_FA["channel_not_found"],
    "telegram_unavailable": "تلگرام در میانه‌ی کار پاسخ نداد. بخشی از پیام‌ها مانده است؛ کمی بعد دوباره بزنید.",
}
