// Posts through the SECOND Telegram account with Premium (decision D38): the owner picks who posts,
// pastes one custom-emoji pack link, and sees which emoji of the next post become animated, the
// account's rights in each channel and the posts that went with the bot instead. Types and endpoints
// of /api/posts/operator, the Persian words for its codes, and a pack-link check that mirrors the
// server's (panel/proxyhub/posts/operator.py). The server is always the authority.

import { request } from "./api";
import { toFaDigits } from "./format";
import type { Tone } from "./labels";
import type { PostRole } from "./posts";

export type PosterName = "bot" | "operator";

export interface ChannelRights {
  status: string | null;
  can_post: boolean | null;
  can_edit: boolean | null;
  can_delete: boolean | null;
  error: string | null;
  telegram_error: string | null;
}

export interface OperatorChannel {
  username: string | null;
  public: boolean;
  /** Why the second account cannot take a NEW post for this channel now (null: it can). */
  ready: string | null;
  rights: ChannelRights | null;
  warnings: string[];
}

export interface PackEmoji {
  emoji: string;
  id: string;
}

export interface OperatorPack {
  short_name: string;
  title: string;
  count: number;
  fetched_at: string | null;
  attempted_at: string | null;
  error: string | null;
  telegram_error: string | null;
  emoji: PackEmoji[];
}

export interface Fallback {
  at: string | null;
  role: PostRole | null;
  reason: string;
  stage: "queue" | "send" | null;
  text_fa: string;
}

/** The channel's own animated emoji pack, made by the bot (panel/proxyhub/posts/own_pack.py). */
export interface OwnPack {
  state: "waiting" | "pending" | "ready" | "error";
  /** Why the bot cannot make it now (kill_switch, no_bot, no_owner, no_public_channel, bad_name). */
  reason: string | null;
  name: string | null;
  link: string | null;
  emoji: number;
  total: number;
  error: string | null;
  checked_at?: string | null;
  /** Telegram asked to wait until then (429); the panel goes on by itself. */
  retry_at?: string | null;
}

export interface OperatorView {
  poster: PosterName;
  emoji_pack: string | null;
  pack_link: string | null;
  account: { state: string; first_name: string | null; kill_switch: boolean };
  hold_until: string | null;
  channels: Partial<Record<PostRole, OperatorChannel>>;
  rights_checked_at: string | null;
  pack: OperatorPack | null;
  fallbacks: Fallback[];
  own_pack?: OwnPack;
  outcome?: string;
}

export interface OperatorInput {
  poster?: PosterName;
  pack_link?: string | null;
}

// --- endpoints ----------------------------------------------------------------------------------------------

const BASE = "/api/posts/operator";

export const getOperator = (signal?: AbortSignal) => request<OperatorView>("GET", BASE, undefined, signal ? { signal } : {});
export const putOperator = (input: OperatorInput) => request<OperatorView>("PUT", BASE, input);
export const checkOperator = () => request<OperatorView>("POST", `${BASE}/check`, {});

// --- the pack link (mirrors the server) -------------------------------------------------------------------------

const PACK_LINK = /^(?:(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me)\/(addemoji|addstickers)\/([^/?#\s]+)\/?|tg:\/\/(addemoji|addstickers)\?set=([^&#\s]+))$/i;
const SHORT_NAME = /^[A-Za-z0-9_]{1,64}$/;

export type PackLinkParsed = { ok: true; shortName: string | null } | { ok: false; error: string };

/** "" -> no pack; "https://t.me/addemoji/<name>" (also tg://addemoji?set=<name>) -> the short name. */
export function parsePackLink(raw: string): PackLinkParsed {
  const s = raw.trim();
  if (s === "") return { ok: true, shortName: null };
  const m = PACK_LINK.exec(s);
  if (!m) return { ok: false, error: "لینک بسته‌ی ایموجی به شکل https://t.me/addemoji/نام‌بسته است." };
  const kind = (m[1] ?? m[3] ?? "").toLowerCase();
  if (kind === "addstickers") return { ok: false, error: "این لینک بسته‌ی استیکر است؛ لینک بسته‌ی ایموجی با t.me/addemoji شروع میشود." };
  const name = m[2] ?? m[4] ?? "";
  if (!SHORT_NAME.test(name)) return { ok: false, error: "نام بسته فقط حرف انگلیسی، رقم و _ است." };
  return { ok: true, shortName: name };
}

// --- words ------------------------------------------------------------------------------------------------------

export const POSTER_LABEL: Record<PosterName, string> = { bot: "ربات", operator: "حساب دوم (Premium)" };

/** Why the second account cannot take a new post now: what the owner reads, and what to do. */
export function readyText(code: string | null): string | null {
  switch (code) {
    case null:
      return null;
    case "kill_switch":
      return "کلید توقف روشن است؛ تا خاموش شدنش پست‌ها با ربات میروند.";
    case "not_connected":
      return "حساب دوم وصل نیست؛ پست‌ها با ربات میروند. حساب را در صفحه‌ی ‎@MTProxybot‎ وصل کنید.";
    case "no_username":
      return "این کانال نام کاربری عمومی (@) ندارد؛ حساب دوم کانال را فقط با نام عمومی پیدا میکند، پس پست‌ها با ربات میروند.";
    case "hold":
      return "تلگرام از حساب دوم خواسته مدتی صبر کند؛ تا آن وقت پست‌ها با ربات میروند.";
    case "no_post_right":
      return "حساب دوم در این کانال اجازه‌ی «ارسال پست» ندارد؛ پست‌ها با ربات میروند.";
    default:
      return `حساب دوم فعلاً پست نمیگذارد (${code})؛ پست‌ها با ربات میروند.`;
  }
}

export const WARNING_TEXT: Record<string, string> = {
  no_post_right: "اجازه‌ی «Post messages» ندارد؛ آن را روشن کنید.",
  no_edit_right: "اجازه‌ی «Edit messages of others» ندارد؛ بی آن علامت‌های وضعیت پست‌ها زده نمیشود.",
  delete_right: "اجازه‌ی «Delete messages» دارد؛ آن را بردارید (پنل هرگز پستی حذف نمیکند، ولی این اجازه نباید باشد).",
};

export function warningText(code: string): string {
  return WARNING_TEXT[code] ?? code;
}

export function rightsStatus(rights: ChannelRights | null): { label: string; tone: Tone } {
  switch (rights?.status ?? null) {
    case null:
      return { label: "هنوز بررسی نشده", tone: "neutral" };
    case "admin":
      return { label: "ادمین", tone: "success" };
    case "creator":
      return { label: "سازنده‌ی کانال", tone: "success" };
    case "member":
      return { label: "عضو، ولی ادمین نیست", tone: "danger" };
    case "not_member":
      return { label: "عضو کانال نیست", tone: "danger" };
    case "channel_not_found":
      return { label: "کانال با این نام پیدا نشد", tone: "danger" };
    case "no_username":
      return { label: "کانال نام عمومی ندارد", tone: "warning" };
    case "banned":
    case "left":
      return { label: "از کانال بیرون است", tone: "danger" };
    case "error":
      return { label: "بررسی به تلگرام نرسید", tone: "warning" };
    default:
      return { label: rights?.status ?? "", tone: "neutral" };
  }
}

/** Why the last read of the pack did not work. */
export function packErrorText(code: string | null, rpc: string | null = null): string | null {
  switch (code) {
    case null:
      return null;
    case "not_emoji_pack":
      return "این بسته، بسته‌ی ایموجی (custom emoji) نیست. لینک یک بسته‌ی ایموجی را بچسبانید.";
    case "telegram_refused":
      return rpc === "STICKERSET_INVALID" ? "تلگرام بسته‌ای با این نام پیدا نکرد. لینک را دوباره کپی کنید." : `تلگرام بسته را نداد (${rpc ?? "?"}).`;
    case "telegram_unavailable":
      return "خواندن بسته به تلگرام نرسید؛ یک ساعت بعد خودکار دوباره امتحان میشود.";
    case "flood_wait":
      return "تلگرام خواست کمی صبر کنیم؛ بعداً خودکار دوباره امتحان میشود.";
    case "session_terminated":
      return "نشست حساب دوم بسته شده است؛ حساب را دوباره وصل کنید.";
    default:
      return `خواندن بسته ناموفق بود (${code}).`;
  }
}

/** One line about the channel's own pack: what the owner reads, and its tone. */
export function ownPackText(p: OwnPack): { tone: Tone; text: string } {
  if (p.state === "ready") {
    const all = p.emoji >= p.total;
    return {
      tone: all ? "success" : "info",
      text: all ? `ساخته شد؛ ${toFaDigits(p.emoji)} ایموجی متحرک.` : `ساخته شد؛ ${toFaDigits(p.emoji)} از ${toFaDigits(p.total)} ایموجی، بقیه خودکار اضافه میشود.`,
    };
  }
  if (p.state === "error" && p.retry_at) return { tone: "info", text: "تلگرام خواست کمی صبر کنیم؛ پنل خودش ادامه میدهد. کاری لازم نیست." };
  if (p.state === "error") return { tone: "warning", text: "ساخت بسته ناموفق بود؛ یک ساعت بعد خودکار دوباره امتحان میشود." };
  switch (p.reason) {
    case "kill_switch":
      return { tone: "neutral", text: "کلید توقف روشن است؛ پس از خاموش شدنش بسته خودکار ساخته میشود." };
    case "no_bot":
      return { tone: "neutral", text: "اول ربات کانال را در صفحه‌ی تلگرام وصل کنید." };
    case "no_owner":
      return { tone: "neutral", text: "اول در صفحه‌ی تلگرام، ربات را با حساب خودتان جفت کنید؛ بسته به نام شما ساخته میشود." };
    case "no_public_channel":
      return { tone: "neutral", text: "کانال اصلی نام کاربری عمومی (@) ندارد؛ عنوان بسته همان نام کانال است." };
    case "bad_name":
      return { tone: "warning", text: "نام کانال یا ربات برای نام بسته مناسب نیست." };
    default:
      return { tone: "info", text: "تا یک دقیقه‌ی دیگر خودکار ساخته میشود." };
  }
}

/** Who takes the next NEW post of this channel (edits always go through the poster that posted). */
export function effectivePoster(v: OperatorView, role: PostRole): PosterName {
  if (v.poster !== "operator") return "bot";
  return v.channels[role]?.ready ? "bot" : "operator";
}
