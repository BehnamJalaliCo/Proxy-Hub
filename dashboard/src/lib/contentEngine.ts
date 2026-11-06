// The content engine (decision D51): the owner's sources (public Telegram channels, RSS feeds) and the
// review queue (decision D20). Types and endpoints of /api/content, the Persian words for its states,
// and checks that mirror the server's (panel/proxyhub/content). The server is always the authority.

import { request } from "./api";
import { formatNumber, toFaDigits } from "./format";
import type { Tone } from "./labels";

export type SourceKind = "telegram" | "rss";

export interface ContentSource {
  id: number;
  kind: SourceKind;
  /** telegram: the channel's username (no @); rss: the feed's https URL. */
  ref: string;
  label: string;
  /** The source's own page (https), opened in a new tab. */
  link: string;
  enabled: boolean;
  created_at: string;
  last_fetch_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  last_error_fa: string | null;
  failures: number;
  next_fetch_at: string | null;
  last_count: number;
  /** Items of this source waiting in the review queue. */
  pending: number;
}

export interface SourcesView {
  sources: ContentSource[];
  max_sources: number;
  fetch_every_min: number;
}

export type CandidateStatus = "pending" | "approved" | "rejected" | "used" | "expired";
export type QueueSort = "new" | "views" | "hot";
export type MediaState = "none" | "waiting" | "ready" | "failed";

export interface CandidateMedia {
  kind: "photo" | "video";
  state: MediaState;
  error: string | null;
  error_fa: string | null;
  /** The panel's own copy to show (a photo, or a video's thumbnail): /api/content/media/<id>. */
  preview_id: number | null;
  width: number | null;
  height: number | null;
  duration: number | null;
  size: number | null;
}

export interface Candidate {
  id: number;
  status: CandidateStatus;
  source: { id: number | null; kind: SourceKind; label: string };
  /** The original post (https). */
  url: string | null;
  published_at: string | null;
  created_at: string;
  reviewed_at: string | null;
  used_at: string | null;
  views: number | null;
  title: string;
  body: string;
  source_text: string;
  /** Other sources that gave the same news. */
  also_in: string[];
  media: CandidateMedia | null;
}

export interface QueueView {
  items: Candidate[];
  counts: Record<CandidateStatus | "duplicate", number>;
  /** How many characters a media post's content may have with the links of the next post (null:
   *  no live link now). */
  caption_budget: number | null;
}

// --- endpoints ---------------------------------------------------------------------------------------------

const opt = (signal?: AbortSignal) => (signal ? { signal } : {});

export const getSources = (signal?: AbortSignal) => request<SourcesView>("GET", "/api/content/sources", undefined, opt(signal));

export const addSource = (input: { kind: SourceKind; ref: string; label?: string }) =>
  request<ContentSource>("POST", "/api/content/sources", input);

export const updateSource = (id: number, input: { enabled?: boolean; label?: string }) =>
  request<ContentSource>("PUT", `/api/content/sources/${String(id)}`, input);

export const fetchSourceNow = (id: number) => request<ContentSource>("POST", `/api/content/sources/${String(id)}/fetch`);

export const removeSource = (id: number) =>
  request<{ removed: boolean; dropped_pending: number }>("DELETE", `/api/content/sources/${String(id)}`);

export const getQueue = (status: CandidateStatus, sort: QueueSort, signal?: AbortSignal) =>
  request<QueueView>("GET", `/api/content/queue?status=${status}&sort=${sort}`, undefined, opt(signal));

export const editCandidate = (id: number, caption: { title: string; body: string }) =>
  request<Candidate>("PUT", `/api/content/candidates/${String(id)}`, caption);

export const setCandidateStatus = (
  id: number,
  status: "pending" | "approved" | "rejected",
  caption?: { title: string; body: string },
) => request<Candidate>("POST", `/api/content/candidates/${String(id)}/status`, caption ? { status, ...caption } : { status });

/** The panel's own copy of a candidate's photo or thumbnail (never the source's address). */
export const mediaUrl = (id: number) => `/api/content/media/${String(id)}`;

// --- checks (mirror panel/proxyhub/content) ------------------------------------------------------------

export const TITLE_MAX = 120;
export const BODY_MAX = 1000;
export const LABEL_MAX = 80;

const TG_INPUT = /^(?:@|(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/(?:s\/)?)?([A-Za-z0-9_]{1,64})\/?$/;
const TG_NAME = /^[a-z][a-z0-9_]{3,30}[a-z0-9]$/;
const TG_RESERVED = new Set(["joinchat", "addemoji", "addstickers", "proxy", "socks", "share", "iv", "login", "setlanguage", "s"]);

/** A public channel's username from "@name", "name" or a t.me link (lower case), or null. */
export function telegramName(raw: string): string | null {
  const m = TG_INPUT.exec(raw.trim());
  const name = m?.[1]?.toLowerCase();
  if (!name || TG_RESERVED.has(name) || name.includes("__") || !TG_NAME.test(name)) return null;
  return name;
}

/** Why the owner's input cannot be a source (Persian), or null when it looks right. */
export function sourceInputError(kind: SourceKind, ref: string, label: string): string | null {
  if (label.trim().length > LABEL_MAX) return "نام نمایشی حداکثر ۸۰ نویسه است.";
  if (kind === "telegram") {
    return telegramName(ref) ? null : "نام عمومی کانال را بنویسید، مثل ‎@bbcpersian‎ یا ‎https://t.me/bbcpersian‎.";
  }
  const value = ref.trim();
  if (!/^https:\/\/[^\s/?#]+\.[^\s/?#]+(?:[/?#]\S*)?$/i.test(value)) return "نشانی RSS باید با https:// شروع شود.";
  const host = value.slice(8).split(/[/?#]/)[0]?.toLowerCase() ?? "";
  if (/^[\d.]+$/.test(host) || host.startsWith("[") || host.includes("@") || host.includes(":")) {
    return "نشانی RSS باید نام دامنه داشته باشد (نه IP، نه پورت دیگر).";
  }
  return null;
}

const EMOJI = /\p{Extended_Pictographic}/u;

/** The characters a caption's content takes in the post as Telegram counts them (UTF-16 units):
 *  the bold title line (with 📡 in front when it has no emoji), an empty line, then the text. */
export function captionLength(title: string, body: string): number {
  const t = title.replace(/\s+/g, " ").trim();
  const b = body.trim();
  const line = t ? (EMOJI.test(t) ? t : `📡 ${t}`) : "";
  return line.length + (line && b ? 2 : 0) + b.length;
}

export function captionErrors(title: string, body: string): { title?: string; body?: string } {
  const out: { title?: string; body?: string } = {};
  if (title.trim().length > TITLE_MAX) out.title = "عنوان حداکثر ۱۲۰ نویسه است.";
  if (body.trim().length > BODY_MAX) out.body = "متن حداکثر ۱۰۰۰ نویسه است.";
  if (!title.trim() && !body.trim()) out.title = "عنوان یا متن لازم است.";
  return out;
}

// --- words -------------------------------------------------------------------------------------------------

export const KIND_LABEL: Record<SourceKind, string> = { telegram: "کانال تلگرام", rss: "RSS" };

export const STATUS_INFO: Record<CandidateStatus, { label: string; tone: Tone; hint: string }> = {
  pending: { label: "در صف", tone: "info", hint: "منتظر نظر شما. تأیید کنید تا محتوای پست بعدی شود، یا رد کنید." },
  approved: {
    label: "تأییدشده",
    tone: "success",
    hint: "به ترتیب تأیید، محتوای پست‌های بعدی میشوند؛ پیش از بانک محتوای قدیمی.",
  },
  rejected: { label: "ردشده", tone: "neutral", hint: "به کانال نمیرود. میتوانید به صف برگردانید." },
  used: { label: "رفته", tone: "brand", hint: "در یک پست رفته است." },
  expired: { label: "کهنه", tone: "warning", hint: "بیش از ۴۸ ساعت از خبر گذشت و دیگر نمیرود." },
};

export const SORT_LABEL: Record<QueueSort, string> = { new: "تازه‌ترین", views: "پربازدیدترین", hot: "داغ‌ترین" };

export const MEDIA_STATE_TEXT: Record<MediaState, string> = {
  none: "",
  waiting: "در حال دانلود",
  ready: "آماده",
  failed: "دانلود نشد",
};

export function sourceHealth(s: ContentSource): { label: string; tone: Tone } {
  if (!s.enabled) return { label: "خاموش", tone: "neutral" };
  if (s.last_error) return { label: "خطا", tone: "danger" };
  if (!s.last_ok_at) return { label: "در انتظار اولین خواندن", tone: "info" };
  return { label: "سالم", tone: "success" };
}

/** 6790 -> «۶٫۸ هزار», 1200000 -> «۱٫۲ میلیون». */
export function viewsText(n: number | null): string | null {
  if (n === null) return null;
  if (n >= 1_000_000) return `${formatNumber(Math.round(n / 100_000) / 10, 1)} میلیون`;
  if (n >= 1_000) return `${formatNumber(Math.round(n / 100) / 10, 1)} هزار`;
  return formatNumber(n);
}

/** 65 -> «۱:۰۵». */
export function durationText(seconds: number | null): string | null {
  if (seconds === null || seconds < 0) return null;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return toFaDigits(`${String(m)}:${String(s).padStart(2, "0")}`);
}

/** «۲٫۴ مگابایت» / «۳۱۰ کیلوبایت». */
export function sizeText(bytes: number | null): string | null {
  if (bytes === null) return null;
  if (bytes >= 1024 * 1024) return `${formatNumber(Math.round((bytes / 1024 / 1024) * 10) / 10, 1)} مگابایت`;
  return `${formatNumber(Math.max(1, Math.round(bytes / 1024)))} کیلوبایت`;
}
