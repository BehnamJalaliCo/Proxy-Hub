"""Owner-facing Persian text of the analytics part: Telegram alerts and API errors."""

from __future__ import annotations

import html
from typing import Final

from proxyhub.telegram.templates import fa_digits

ERRORS_FA: Final[dict[str, str]] = {
    "invalid_request": "درخواست ناقص یا نادرست است.",
    "invalid_settings": (
        "تنظیمات درست نیست. آستانه‌ی ترافیک و CPU باید عددی بین ۱ تا ۱۰۰ درصد باشد "
        "و بازه‌ی CPU بین ۱۵ تا ۳۶۰ دقیقه."
    ),
    "invalid_link_name": ("نام لینک باید ۱ تا ۳۲ نویسه باشد (سقف تلگرام). توضیح هم حداکثر ۲۰۰ نویسه است."),
    "link_name_taken": "لینکی با همین نام هست. نام دیگری انتخاب کنید تا آمار دو کمپین قاطی نشود.",
    "link_not_found": "این لینک ردیابی پیدا نشد.",
    "bot_not_set": "هنوز توکن ربات تلگرام وارد نشده است. اول ربات را در صفحه‌ی تلگرام وصل کنید.",
    "main_channel_not_set": "کانال اصلی هنوز ثبت نشده است. اول کانال اصلی را در صفحه‌ی تلگرام ثبت کنید.",
    "bot_cannot_invite": (
        "ربات در کانال اصلی اجازه‌ی ساخت لینک دعوت ندارد. در تنظیمات ادمین ربات، «دعوت کاربران با لینک» "
        "را روشن کنید و دوباره امتحان کنید."
    ),
    "bot_token_invalid": "توکن ربات درست نیست یا در BotFather باطل شده است.",
    "telegram_unavailable": "پاسخ تلگرام به دست نیامد. چند دقیقه‌ی دیگر دوباره امتحان کنید.",
    "vault_unavailable": "کلید رمزنگاری پنل در دسترس نیست؛ کار انجام نشد.",
}


def _pct(value: float) -> str:
    return fa_digits(f"{value:.0f}")


def traffic_alert_fa(node_name: str, pct: float, threshold: int, used_tb: float, included_tb: float) -> str:
    """Telegram HTML: one server is close to its included traffic for this billing period."""
    used, included = fa_digits(f"{used_tb:.2f}"), fa_digits(f"{included_tb:.0f}")
    return (
        "🟠 <b>ترافیک یک سرور نزدیک سقف رایگان است</b>\n"
        f"سرور <code>{html.escape(node_name)}</code> تا الان {_pct(pct)}٪ "
        "از ترافیک رایگان این ماهش را مصرف کرده "
        f"({used} از {included} ترابایت؛ آستانه‌ی هشدار {_pct(threshold)}٪).\n"
        "بیشتر از سقف، هر ترابایت هزینه‌ی جدا دارد. جزئیات در صفحه‌ی «آمار» پنل است."
    )


def cpu_alert_fa(node_name: str, avg: float, threshold: int, window_min: int) -> str:
    """Telegram HTML: one server's CPU stayed high for the whole window."""
    return (
        "🟠 <b>CPU یک سرور مدتی است بالاست</b>\n"
        f"میانگین CPU سرور <code>{html.escape(node_name)}</code> در {fa_digits(window_min)} دقیقه‌ی گذشته "
        f"{_pct(avg)}٪ بوده است (آستانه‌ی هشدار {_pct(threshold)}٪).\n"
        "ممکن است کاربران کندی ببینند. جزئیات در صفحه‌ی «آمار» پنل است."
    )
