"""HTTP conventions of the API contract, for the fleet team's routers.

* Errors are ``{"error": "<code>", "message_fa": "<Persian sentence>", ...}``.
* Every non-GET request must carry ``X-Requested-With: proxyhub``, else 403 ``{"error": "csrf"}``.
  This is checked here as well as by the auth layer (defence in depth; it costs nothing).
* Request validation errors are answered WITHOUT echoing the submitted values, so a token sent
  in a malformed body can never come back in a response (CLAUDE.md rule 3).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "proxyhub"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

MESSAGES_FA: dict[str, str] = {
    "csrf": "درخواست بدون سرآیند امنیتی رسید و رد شد. صفحه را دوباره باز کنید.",
    "invalid_input": "داده‌ی فرستاده‌شده درست نیست.",
    "invalid_name": "نام حساب درست نیست: ۱ تا ۱۰۰ نویسه، فقط حرف، رقم، فاصله، نقطه، خط تیره و زیرخط.",
    "token_invalid": "توکن هتزنر معتبر نیست یا باطل شده است.",
    "token_read_only": (
        "این توکن فقط‌خواندنی است. در همان project یک توکن «Read & Write» بسازید و آن را وارد کنید."
    ),
    "name_taken": "این نام قبلاً برای حساب دیگری به کار رفته است. نام دیگری انتخاب کنید.",
    "account_not_empty": (
        "در این project سروری هست که مال این سیستم نیست. توکن پذیرفته و ذخیره نشد و حساب قفل شد. "
        "فقط توکن یک project خالی و جدا برای nodeها را وارد کنید."
    ),
    "hetzner_unavailable": "پاسخ API هتزنر به دست نیامد. چند دقیقه‌ی دیگر دوباره امتحان کنید.",
    "not_found": "حساب پیدا نشد.",
    "illegal_transition": "این تغییر وضعیت برای این حساب در وضعیت فعلی‌اش مجاز نیست.",
    "account_has_nodes": "این حساب هنوز node زنده دارد. اول باید همه‌ی nodeهایش بازنشسته و حذف شوند.",
    "invalid_settings": "تنظیمات درست نیست. بخش‌های مشخص‌شده را اصلاح کنید.",
    "live_mode_locked": (
        "حالت واقعی ناوگان در این نسخه قفل است و فقط حالت خشک (dry-run) ممکن است. "
        "روشن کردن آن تصمیم جداگانه‌ی مالک در مرحله‌ی بعد است."
    ),
    "settings_corrupt": "تنظیمات ذخیره‌شده‌ی ناوگان خراب است؛ تا اصلاح آن هیچ سروری ساخته نمی‌شود.",
    "vault_unavailable": "کلید رمزنگاری پنل در دسترس نیست؛ توکن ذخیره نشد.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra

    def response(self) -> JSONResponse:
        return error_response(self.status, self.code, **self.extra)


def error_response(status: int, code: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": code, "message_fa": MESSAGES_FA.get(code, MESSAGES_FA["invalid_input"])}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def validation_fields(errors: Any) -> list[dict[str, Any]]:
    """Location and type of each error, never the submitted input."""
    out: list[dict[str, Any]] = []
    for err in errors:
        out.append({"loc": [str(p) for p in err.get("loc", ())], "type": str(err.get("type", ""))})
    return out


class ContractRoute(APIRoute):
    """APIRoute that applies the contract's CSRF rule and error format to its endpoint."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def contract_handler(request: Request) -> Response:
            if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return error_response(403, "csrf")
            try:
                return await handler(request)
            except ApiError as exc:
                return exc.response()
            except RequestValidationError as exc:
                return error_response(422, "invalid_input", fields=validation_fields(exc.errors()))

        return contract_handler
