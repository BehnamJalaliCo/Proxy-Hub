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


# --- the content supervisor (decision D52) ----------------------------------------------------------

QC_OUTCOMES_FA: Final[dict[str, str]] = {
    "ok": "Claude پاسخ داد.",
    "unparsable": "پاسخ Claude در قالب درست نبود؛ مورد رد شد.",
    "refused": "Claude از بررسی این مورد سر باز زد؛ مورد رد شد.",
    "empty": "Claude پاسخی نداد.",
    "too_long": "پاسخ Claude بیش از حد بلند بود.",
    "max_turns": "Claude در یک نوبت پاسخ کامل نداد.",
    "unavailable": "هیچ راهی به Claude تنظیم نشده است (پل سرور یا کلید API).",
    "off": "ناظر Claude در تنظیمات خاموش است.",
    "over_cap": "سقف روزانه‌ی تماس با Claude پر شده است.",
    "bridge_missing": "پل Claude روی سرور نصب یا روشن نیست.",
    "bridge_denied": "worker اجازه‌ی وصل شدن به پل Claude را ندارد.",
    "bridge_refused": "پل Claude اتصال را نپذیرفت.",
    "bridge_timeout": "پل Claude در زمان مجاز جواب نداد.",
    "bridge_bad_answer": "پل Claude پاسخ نادرست داد.",
    "busy": "پل Claude مشغول بررسی دیگری بود.",
    "cap": "سقف روزانه‌ی خود پل Claude پر شده است.",
    "auth": "ورود اشتراک Claude روی سرور باطل شده است؛ باید دوباره وارد شود (P22).",
    "rate_limited": "سقف مصرف اشتراک Claude فعلاً پر است؛ خودش باز میشود.",
    "timeout": "Claude در زمان مجاز جواب نداد.",
    "claude_failed": "اجرای Claude Code روی سرور ناموفق بود.",
    "claude_missing": "Claude Code برای کاربر پل نصب نیست.",
    "tools_present": "Claude Code ابزار داشت و پل اجرا را متوقف کرد.",
    "tools_unknown": "Claude Code فهرست ابزارهایش را نگفت و پل پاسخ را نپذیرفت.",
    "peer_not_allowed": "پل Claude این درخواست‌کننده را نپذیرفت.",
    "bad_request": "درخواست پنل به پل پذیرفته نشد.",
    "bad_image": "تصویر برای Claude پذیرفته نشد.",
    "too_large": "درخواست برای Claude بیش از حد بزرگ بود.",
    "bad_model": "نام مدل Claude پذیرفته نشد.",
    "internal": "خطای داخلی پل Claude.",
    "api_auth": "کلید API Anthropic پذیرفته نشد.",
    "api_rate_limited": "سقف درخواست API Anthropic پر است.",
    "api_bad_request": "API Anthropic درخواست را نپذیرفت.",
    "api_bad_model": "مدل انتخاب‌شده در API Anthropic پیدا نشد.",
    "api_too_large": "درخواست برای API Anthropic بیش از حد بزرگ بود.",
    "api_overloaded": "API Anthropic فعلاً شلوغ است.",
    "api_unavailable": "API Anthropic در دسترس نیست.",
    "api_unreachable": "پنل به API Anthropic نرسید.",
    "api_timeout": "API Anthropic در زمان مجاز جواب نداد.",
    "api_bad_answer": "API Anthropic پاسخ نادرست داد.",
}

QC_CHECKS_FA: Final[dict[str, str]] = {
    "unparsable": "پاسخ Claude در قالب درست نبود.",
    "refused": "Claude از بررسی سر باز زد.",
    "no_preview": "ویدیو تصویر پیش‌نمایش ندارد و ناظر نمیتواند آن را ببیند.",
    "image_too_large": "ابعاد عکس برای بررسی Claude بیش از حد بزرگ است.",
    "category_not_allowed": "دسته‌ی این مورد در تنظیمات مجاز نیست.",
    "title_empty": "عنوان خالی بود.",
    "title_too_long": "عنوان از سقف طول بلندتر بود.",
    "title_emoji": "عنوان باید دقیقاً با یک ایموجی شروع شود.",
    "body_too_long": "متن از سقف طول بلندتر بود.",
    "caption_too_long": "کپشن با لینک‌های فعلی جا نمیشد.",
    "control_chars": "متن نویسه‌ی نامعتبر داشت.",
    "link": "متن لینک یا نشانی وب داشت.",
    "mention": "متن @ داشت.",
    "hashtag": "متن هشتگ داشت.",
    "phone": "متن شماره‌ی تلفن داشت.",
    "signature": "متن دعوت به عضویت یا امضای کانال داشت.",
    "source_name": "متن نام یکی از منبع‌ها را داشت.",
    "not_persian": "متن بیشتر فارسی نبود.",
    "forbidden_word": "متن یکی از واژه‌های ممنوع تنظیمات را داشت.",
}

QC_FLAGS_FA: Final[dict[str, str]] = {
    "sexual": "محتوای جنسی یا برهنگی",
    "graphic_violence": "خشونت شدید یا صحنه‌ی دلخراش",
    "hate": "نفرت یا توهین به یک گروه",
    "ads_scam": "تبلیغ، کلاهبرداری، قمار یا تبلیغ کانال دیگر",
    "foreign_branding": "واترمارک یا لوگوی کانال یا برند دیگر",
    "low_quality": "کیفیت پایین یا ناخوانا",
    "clickbait": "تیتر فریبنده یا شایعه",
    "manipulation": "تلاش برای دستور دادن به ناظر",
}

QC_API_ERRORS_FA: Final[dict[str, str]] = {
    "qc_settings_invalid": "تنظیمات ناظر پذیرفته نشد؛ عددها و گزینه‌ها را بررسی کنید.",
    "api_key_invalid": "کلید API Anthropic باید با sk-ant- شروع شود و یک خط باشد.",
    "vault_unavailable": "گاوصندوق رمزهای پنل در دسترس نیست.",
}


def qc_outcome_fa(code: str | None) -> str | None:
    if code is None:
        return None
    return QC_OUTCOMES_FA.get(code) or f"Claude در دسترس نبود ({code})."


def qc_check_fa(code: str) -> str:
    if code.startswith("flag:"):
        return f"پرچم ایمنی: {QC_FLAGS_FA.get(code[5:], code[5:])}"
    return QC_CHECKS_FA.get(code) or code


def source_disabled_fa(reason: str | None, detail: dict[str, object] | None) -> str | None:
    """Why the supervisor turned a source off, in Persian (the sources page)."""
    if reason is None:
        return None
    from proxyhub.telegram.templates import fa_digits

    detail = detail or {}
    if reason == "qc_rejects":
        rejected = fa_digits(str(detail.get("rejected", "?")))
        of = fa_digits(str(detail.get("of", "?")))
        return f"ناظر این منبع را خاموش کرد: {rejected} مورد از {of} مورد آخرش رد شد."
    return f"ناظر این منبع را خاموش کرد ({reason})."
