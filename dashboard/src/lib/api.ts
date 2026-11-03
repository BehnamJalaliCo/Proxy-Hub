// The one place that talks to the panel API. Every call is same-origin with the session cookie and
// carries "X-Requested-With: proxyhub" (the CSRF rule of the API contract). A 401 sends the owner to
// /login. Nothing here ever logs a request body: it can hold a password, a TOTP code or a token.

export const CSRF_HEADER = "X-Requested-With";
export const CSRF_VALUE = "proxyhub";

const FALLBACK_FA: Record<number, string> = {
  0: "اتصال به پنل برقرار نشد. اینترنت یا در دسترس بودن سرور را بررسی کنید.",
  400: "درخواست پذیرفته نشد. ورودی‌ها را بررسی کنید.",
  401: "برای دیدن این بخش دوباره وارد پنل شوید.",
  403: "اجازه‌ی این کار را ندارید. صفحه را دوباره باز کنید.",
  404: "این بخش هنوز روی سرور پنل فعال نشده یا پیدا نشد.",
  409: "این کار با وضعیت فعلی سازگار نیست.",
  422: "داده‌ی فرستاده‌شده درست نیست.",
  429: "درخواست‌ها زیاد بود. کمی بعد دوباره امتحان کنید.",
  500: "خطای داخلی در سرور پنل رخ داد.",
  502: "سرویس بیرونی پاسخ نداد. کمی بعد دوباره امتحان کنید.",
  503: "سرویس پنل هنوز آماده نیست یا موقتاً در دسترس نیست.",
};

export function fallbackMessage(status: number): string {
  if (status in FALLBACK_FA) return FALLBACK_FA[status] ?? "";
  return status >= 500 ? (FALLBACK_FA[500] ?? "") : (FALLBACK_FA[400] ?? "");
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly messageFa: string;
  readonly body: Record<string, unknown>;

  constructor(status: number, code: string, messageFa: string, body: Record<string, unknown> = {}) {
    super(`${String(status)} ${code}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.messageFa = messageFa;
    this.body = body;
  }

  /** Extra numeric field of the error body (retry_after_s, foreign_servers, ...). */
  num(key: string): number | null {
    const v = this.body[key];
    return typeof v === "number" && Number.isFinite(v) ? v : null;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Persian text for any thrown value, for toasts and error states. */
export function errorMessage(e: unknown): string {
  if (isApiError(e)) return e.messageFa;
  return fallbackMessage(0);
}

/** Turns a non-2xx response body into an ApiError. Works for the contract shape and for plain
 * FastAPI errors ({"detail": ...}) that do not follow it yet. */
export function toApiError(status: number, payload: unknown): ApiError {
  if (payload !== null && typeof payload === "object" && !Array.isArray(payload)) {
    const body = payload as Record<string, unknown>;
    const code = typeof body["error"] === "string" ? body["error"] : `http_${String(status)}`;
    const msg =
      typeof body["message_fa"] === "string" && body["message_fa"].trim() !== ""
        ? body["message_fa"]
        : fallbackMessage(status);
    return new ApiError(status, code, msg, body);
  }
  return new ApiError(status, `http_${String(status)}`, fallbackMessage(status));
}

type Unauthorized = () => void;
let onUnauthorized: Unauthorized | null = null;

/** The app registers how to react to a 401 (clear caches, go to /login). */
export function setUnauthorizedHandler(handler: Unauthorized | null): void {
  onUnauthorized = handler;
}

export interface RequestOptions {
  /** Login and setup answer 401/404 as part of their own flow: no redirect for them. */
  noAuthRedirect?: boolean;
  signal?: AbortSignal;
}

export async function request<T>(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", [CSRF_HEADER]: CSRF_VALUE };
  const init: RequestInit = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (options.signal) init.signal = options.signal;

  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "network", fallbackMessage(0));
  }

  if (res.status === 204) return undefined as T;

  let payload: unknown = null;
  const text = await res.text();
  if (text !== "") {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!res.ok) {
    const err = toApiError(res.status, payload);
    if (res.status === 401 && !options.noAuthRedirect) onUnauthorized?.();
    throw err;
  }
  return payload as T;
}

/** Only same-site paths are accepted as a post-login destination (no open redirect). */
export function safeNext(next: string | null | undefined): string {
  if (!next?.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  if (next.startsWith("/login") || next.startsWith("/setup")) return "/";
  return next;
}
