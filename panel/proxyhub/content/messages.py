"""Persian texts of the content engine: a source's or a media's last error, and the API's errors.

Codes are short and stored; the dashboard shows the Persian text. Nothing here holds fetched content.
"""

from __future__ import annotations

from typing import Final

FETCH_ERRORS_FA: Final[dict[str, str]] = {
    "bad_url": "نشانی پذیرفته نشد: فقط https، روی پورت ۴۴۳ و با نام دامنه.",
    "blocked_address": "این نشانی به یک شبکه‌ی داخلی یا نشانی غیرعمومی میرسد و پنل به آن وصل نمیشود.",
    "dns_failed": "نام این سایت پیدا نشد (DNS).",
    "connect_failed": "اتصال به سایت برقرار نشد.",
    "tls_failed": "گواهی امنیتی (TLS) سایت معتبر نبود.",
    "timeout": "سایت در زمان مجاز جواب نداد.",
    "too_big": "پاسخ سایت از سقف حجم پنل بزرگ‌تر بود.",
    "too_many_redirects": "سایت بیش از حد به جای دیگر ارجاع داد.",
    "bad_redirect": "سایت ارجاع نادرست داد.",
    "bad_encoding": "فشرده‌سازی پاسخ سایت پشتیبانی نمیشود.",
    "bad_status": "سایت پاسخ نامعتبر داد.",
    "bad_content_type": "نوع پاسخ سایت با این نوع منبع جور نیست.",
    "not_public": "این کانال عمومی نیست یا پیش‌نمایش وب آن خاموش است (t.me/s/نام باز نمیشود).",
    "feed_invalid": "این نشانی خوراک RSS یا Atom درستی نداد.",
    "feed_unsafe": "خوراک بخش‌های ناامن XML (DTD یا entity) داشت و خوانده نشد.",
    "parse_failed": "صفحه‌ی منبع خوانده نشد.",
    "internal": "خطای داخلی هنگام خواندن منبع؛ دور بعد دوباره امتحان میشود.",
}

MEDIA_ERRORS_FA: Final[dict[str, str]] = {
    "no_url": "منبع نشانی دانلود این رسانه را نداد (مثلاً ویدیوی بزرگ‌تر از پیش‌نمایش وب).",
    "too_big": "رسانه از سقف حجم پنل بزرگ‌تر است (عکس ۵ مگابایت، ویدیو ۲۰ مگابایت).",
    "unknown_type": "این فایل عکس JPEG یا PNG یا ویدیوی MP4 نیست.",
    "wrong_type": "نوع فایل با نوع رسانه‌ی پست جور نیست.",
    "image_broken": "فایل عکس خراب است.",
    "image_shape": "ابعاد عکس برای تلگرام مجاز نیست (جمع طول و عرض حداکثر ۱۰۰۰۰ و نسبت حداکثر ۲۰).",
    "video_not_mp4": "ویدیو MP4 نیست (مثلاً MOV)؛ تلگرام آن را ویدیو نشان نمیدهد.",
    "video_no_moov": "فایل ویدیو کامل نیست.",
    "video_too_long": "ویدیو بیش از حد طولانی است.",
    "empty": "فایل خالی بود.",
    "storage_full": "جای رسانه در پنل پر است؛ پس از رفتن پست‌های قبلی دوباره امتحان میشود.",
}

API_ERRORS_FA: Final[dict[str, str]] = {
    "invalid_request": "درخواست درست نیست.",
    "kind_invalid": "نوع منبع یا «کانال تلگرام» است یا «RSS».",
    "telegram_name_invalid": (
        "نام کانال درست نیست. نام عمومی کانال را بنویسید، مثل ‎@bbcpersian‎ یا لینک ‎https://t.me/bbcpersian‎."
    ),
    "feed_url_invalid": "نشانی RSS باید با https:// شروع شود و به یک نام دامنه‌ی عمومی برود.",
    "label_invalid": "نام نمایشی حداکثر ۸۰ نویسه و یک خط است.",
    "source_exists": "این منبع قبلاً اضافه شده است.",
    "too_many_sources": "حداکثر ۵۰ منبع میشود اضافه کرد. یکی را حذف کنید.",
    "source_not_found": "این منبع پیدا نشد.",
    "candidate_not_found": "این مورد پیدا نشد.",
    "caption_invalid": "عنوان حداکثر ۱۲۰ نویسه و متن حداکثر ۱۰۰۰ نویسه است؛ عنوان یا متن لازم است.",
    "status_invalid": "این وضعیت برای این مورد پذیرفته نیست.",
    "not_editable": "این مورد دیگر قابل ویرایش نیست (رفته، کهنه شده یا تکراری است).",
    "media_not_found": "این رسانه دیگر در پنل نیست.",
}


def error_fa(code: str) -> str:
    known = FETCH_ERRORS_FA.get(code)
    if known is not None:
        return known
    if code.startswith("http_") and code[5:].isdigit():
        return f"سایت خطای HTTP {code[5:]} داد."
    return f"منبع خوانده نشد ({code})."


def media_error_fa(code: str | None) -> str | None:
    if code is None:
        return None
    return MEDIA_ERRORS_FA.get(code) or error_fa(code)
