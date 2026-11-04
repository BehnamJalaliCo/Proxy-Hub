// The operator account for @MTProxybot (decision D37: a SECOND Telegram account, never the owner's
// personal one). Pure, unit-tested helpers: input checks that mirror the server's
// (panel/proxyhub/mtproxy/validation.py), and the Persian words for its states. The server is always
// the authority. No helper here ever puts an api_hash, a code, a password or a phone into a message.

import type { Tone } from "./labels";
import type { MtpAccount, MtpState } from "./types";
import { asciiDigits, type Parsed } from "./validation";

const API_ID = /^[1-9]\d{0,9}$/;
const API_ID_MAX = 2 ** 31 - 1;
const API_HASH = /^[0-9a-f]{32}$/;
const E164 = /^\+[1-9]\d{6,14}$/;
const SEPARATORS = /[\s\-().‌‎‏‪-‮]/g;

/** A Persian hint if this cannot be an api_id, else null. */
export function apiIdProblem(raw: string): string | null {
  const s = asciiDigits(raw.trim());
  if (s === "") return "api_id را از my.telegram.org بچسبانید (یک عدد است).";
  if (!API_ID.test(s) || Number(s) > API_ID_MAX) return "api_id فقط عدد است؛ همان «App api_id» در my.telegram.org.";
  return null;
}

export function apiHashProblem(raw: string): string | null {
  const s = raw.trim().toLowerCase();
  if (s === "") return "api_hash را از my.telegram.org بچسبانید.";
  if (!API_HASH.test(s)) return "api_hash دقیقاً ۳۲ نویسه از رقم‌های 0 تا 9 و حرف‌های a تا f است.";
  return null;
}

/** "+44 7700 900123", "0044…", Persian digits -> "+447700900123"; anything else a hint. */
export function normalizePhone(raw: string): Parsed<string> {
  let s = asciiDigits(raw).replace(SEPARATORS, "");
  if (s.startsWith("00")) s = `+${s.slice(2)}`;
  if (s === "") return { ok: false, error: "شماره‌ی حساب دوم را با + و کد کشور وارد کنید." };
  if (!s.startsWith("+")) return { ok: false, error: "شماره باید با + و کد کشور شروع شود (مثلاً ‎+44‎ یا ‎+98‎)." };
  if (!E164.test(s)) return { ok: false, error: "این شکل شماره‌ی تلفن نیست: + و کد کشور و ۷ تا ۱۵ رقم." };
  return { ok: true, value: s };
}

/** The login code: 5 to 8 digits, spaces and dashes dropped, Persian digits allowed. */
export function normalizeCode(raw: string): Parsed<string> {
  const s = asciiDigits(raw).replace(/[\s-]/g, "");
  if (s === "") return { ok: false, error: "کدی را که تلگرام فرستاده وارد کنید." };
  if (!/^\d{5,8}$/.test(s)) return { ok: false, error: "کد ورود فقط رقم است (معمولاً ۵ رقم)." };
  return { ok: true, value: s };
}

export function passwordProblem(raw: string): string | null {
  if (raw === "") return "رمز دومرحله‌ای حساب دوم را وارد کنید.";
  if (raw.length > 256) return "رمز دومرحله‌ای حداکثر ۲۵۶ نویسه است.";
  return null;
}

// --- words -------------------------------------------------------------------------------------------

export const MTP_STATE: Record<MtpState, { label: string; tone: Tone }> = {
  none: { label: "وصل نیست", tone: "warning" },
  code_sent: { label: "منتظر کد ورود", tone: "info" },
  password_needed: { label: "منتظر رمز دومرحله‌ای", tone: "info" },
  connected: { label: "وصل", tone: "success" },
  disconnected: { label: "نشست بسته شد", tone: "danger" },
  error: { label: "مشکل دارد", tone: "danger" },
};

export function mtpStateInfo(state: string): { label: string; tone: Tone } {
  return (MTP_STATE as Record<string, { label: string; tone: Tone } | undefined>)[state] ?? { label: state, tone: "neutral" };
}

/** Where Telegram sent the login code. */
export function codeViaText(via: string | null | undefined): string {
  switch (via) {
    case "app":
      return "در اپ تلگرام آمد: در چت Telegram روی دستگاهی که حساب دوم در آن باز است.";
    case "sms":
      return "با پیامک به سیم‌کارت حساب دوم آمد.";
    case "call":
      return "با تماس تلفنی به شماره‌ی حساب دوم گفته میشود.";
    case "email":
      return "به ایمیل ورود حساب دوم آمد.";
    case "fragment":
      return "در Fragment (برای شماره‌های بی‌سیم‌کارت) آمد.";
    case "sms_word":
      return "با پیامک آمد.";
    default:
      return "تلگرام آن را برای حساب دوم فرستاد.";
  }
}

/** Why a check of the stored session did not pass. */
export function checkErrorText(code: string | null | undefined): string | null {
  switch (code) {
    case null:
    case undefined:
      return null;
    case "telegram_unavailable":
      return "بررسی آخر به تلگرام نرسید؛ بررسی بعدی خودکار دوباره امتحان میکند.";
    case "flood_wait":
      return "تلگرام برای بررسی آخر خواست کمی صبر کنیم؛ بررسی بعدی خودکار است.";
    case "mtproxybot_not_found":
      return "این حساب ‎@MTProxybot‎ را پیدا نکرد. حساب را در اپ تلگرام باز کنید و ‎@MTProxybot‎ را یک بار جست‌وجو کنید.";
    case "session_unreadable":
      return "نشست ذخیره‌شده با کلید فعلی پنل باز نمیشود (کلید پنل عوض شده). «قطع اتصال» را بزنید و از اول وارد شوید؛ نشست قدیمی را هم در اپ تلگرام از بخش دستگاه‌ها (Devices) ببندید.";
    case "session_missing":
      return "نشست در گاوصندوق پنل نیست. «قطع اتصال» را بزنید و از اول وارد شوید.";
    case "api_id_invalid":
      return "تلگرام دیگر این api_id را نمیپذیرد. «قطع اتصال» را بزنید و با api_id تازه وارد شوید.";
    default:
      return `بررسی آخر ناموفق بود (${code}).`;
  }
}

/** Why Telegram ended the session (``disconnect_reason``, lower-case Telegram error name). */
export function disconnectReasonText(reason: string | null | undefined): string {
  switch (reason) {
    case "session_revoked":
    case "auth_key_unregistered":
      return "نشست پنل در اپ تلگرام از بخش دستگاه‌ها، یا با «بستن همه‌ی نشست‌های دیگر»، بسته شد.";
    case "user_deactivated":
      return "حساب دوم حذف شده است.";
    case "user_deactivated_ban":
      return "تلگرام حساب دوم را مسدود کرده است.";
    case "session_expired":
      return "نشست منقضی شد.";
    default:
      return "تلگرام میگوید نشست پنل دیگر معتبر نیست.";
  }
}

/** The login in progress has its code or password step open now. */
export function isPending(s: MtpAccount): boolean {
  return s.state === "code_sent" || s.state === "password_needed";
}

/** The new account form is shown for these states (no live session is stored). */
export function canStartLogin(s: MtpAccount): boolean {
  return s.state === "none" || s.state === "disconnected";
}
