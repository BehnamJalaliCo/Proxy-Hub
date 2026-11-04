"""Owner-facing error messages (Persian) and HTTP status per API error code."""

from __future__ import annotations

MESSAGES_FA: dict[str, str] = {
    "csrf": "درخواست از صفحه‌ی پنل نیامده است. صفحه را دوباره باز کنید.",
    "unauthenticated": "برای دیدن این بخش دوباره وارد پنل شوید.",
    "invalid_credentials": "نام کاربری، رمز یا کد تأیید درست نیست.",
    "locked": "به‌خاطر تلاش‌های ناموفق پیاپی، ورود موقتاً بسته شده است. پس از زمان گفته‌شده دوباره امتحان کنید.",
    "setup_token_invalid": "لینک راه‌اندازی نامعتبر است، زمانش گذشته یا پیش‌تر استفاده شده است.",
    "invalid_username": (
        "نام کاربری باید ۳ تا ۳۲ نویسه باشد و فقط حروف کوچک انگلیسی، رقم، نقطه، خط تیره یا زیرخط داشته باشد."
    ),
    "weak_password": (
        "رمز باید دست‌کم ۱۲ نویسه باشد، از رمزهای رایج نباشد و نام کاربری یا نام پنل را در خود نداشته باشد."
    ),
    "totp_invalid": "کد برنامه‌ی Authenticator درست نیست یا پیش‌تر استفاده شده است. کد تازه را وارد کنید.",
    "not_found": "این جلسه پیدا نشد یا پیش‌تر بسته شده است.",
    "invalid_request": "درخواست ناقص یا نادرست است.",
}

STATUS: dict[str, int] = {
    "csrf": 403,
    "unauthenticated": 401,
    "invalid_credentials": 401,
    "locked": 429,
    "setup_token_invalid": 404,
    "invalid_username": 400,
    "weak_password": 400,
    "totp_invalid": 400,
    "not_found": 404,
    "invalid_request": 422,
}
