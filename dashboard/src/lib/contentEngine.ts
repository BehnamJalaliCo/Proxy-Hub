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
  /** D52: why the supervisor turned this source off (null: on, or turned off by the owner). */
  disabled_reason: string | null;
  disabled_reason_fa: string | null;
  disabled_at: string | null;
  /** The supervisor's last decisions on this source's items (since its quality was last reset). */
  quality: { decided: number; rejected: number } | null;
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

/** The content supervisor's review of a candidate (decision D52). */
export interface CandidateQc {
  decision: "approve" | "reject" | null;
  /** Claude's own reasons, Persian. */
  reasons: string[];
  category: string | null;
  /** The safety flags Claude reported. */
  flags: string[];
  /** Deterministic checks that failed (codes, and their Persian text). */
  checks: string[];
  checks_fa: string[];
  error: string | null;
  error_fa: string | null;
  backend: "bridge" | "api" | "rules" | null;
  model: string | null;
  at: string | null;
  tries: number;
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
  /** Who approved or rejected it: the supervisor ("qc") or the owner. */
  decided_by: "owner" | "qc" | null;
  qc: CandidateQc | null;
  media: CandidateMedia | null;
}

export interface QueueView {
  items: Candidate[];
  counts: Record<CandidateStatus | "duplicate", number>;
  /** How many characters a media post's content may have with the links of the next post (null:
   *  no live link now). */
  caption_budget: number | null;
}

export type QcMode = "auto" | "review";
export type QcBackend = "auto" | "bridge" | "api" | "off";
export type ApiModel = "claude-sonnet-5-5" | "claude-haiku-5-5";

export interface QcBlockFlags {
  sexual: boolean;
  graphic_violence: boolean;
  hate: boolean;
  ads_scam: boolean;
  foreign_branding: boolean;
  low_quality: boolean;
  clickbait: boolean;
}

/** The supervisor's settings (panel/proxyhub/content/qc_settings.py). */
export interface QcConfig {
  mode: QcMode;
  backend: QcBackend;
  bridge_model: string;
  api_model: ApiModel;
  bridge_daily_cap: number;
  api_daily_cap: number;
  per_slot: number;
  lead_minutes: number;
  timeout_s: number;
  categories: string[];
  block: QcBlockFlags;
  forbidden_words: string[];
  source_window: number;
  source_min: number;
  source_max_reject_pct: number;
  alert_after_hours: number;
}

export interface BridgeHealth {
  ok: boolean;
  error?: string;
  calls_24h?: number | null;
  daily_cap?: number | null;
  busy?: boolean | null;
  logged_in?: boolean | null;
  version_ok?: boolean | null;
  claude_version?: string | null;
  last_outcome?: string | null;
  rate_limit?: { status?: string; type?: string; resets_at?: number; utilization?: number } | null;
  at?: string;
}

export interface QcView {
  config: QcConfig;
  defaults: QcConfig;
  options: { categories: string[]; flags: (keyof QcBlockFlags)[]; api_models: ApiModel[] };
  api_key_set: boolean;
  calls_24h: { bridge: number; api: number };
  decisions_24h: { approve: number; reject: number };
  /** Approved items waiting for a post. */
  ready: number;
  bridge: BridgeHealth | null;
  bridge_error_fa: string | null;
  last_ok_at: string | null;
  last_outcome: string | null;
  last_outcome_fa: string | null;
  last_outcome_at: string | null;
  unavailable_since: string | null;
  unavailable_reason_fa: string | null;
  slot: { key: string | null; calls: number };
  alerts: string[];
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

export const getQc = (signal?: AbortSignal) => request<QcView>("GET", "/api/content/qc", undefined, opt(signal));

export const saveQc = (config: QcConfig) => request<QcView>("PUT", "/api/content/qc", config);

/** The Anthropic API key goes to the panel's vault; it is never shown again. */
export const setApiKey = (key: string) => request<{ api_key_set: boolean }>("PUT", "/api/content/qc/api-key", { key });

export const removeApiKey = () => request<{ api_key_set: boolean; removed: boolean }>("DELETE", "/api/content/qc/api-key");

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
  pending: {
    label: "در صف",
    tone: "info",
    hint: "ناظر پیش از هر پست بهترین‌ها را بررسی میکند. شما هم میتوانید تأیید یا رد کنید؛ لازم نیست.",
  },
  approved: {
    label: "تأییدشده",
    tone: "success",
    hint: "با تأیید ناظر یا شما، به ترتیب محتوای پست‌های بعدی میشوند؛ پیش از بانک محتوا.",
  },
  rejected: { label: "ردشده", tone: "neutral", hint: "به کانال نمیرود. دلیل ناظر روی هر مورد است؛ میتوانید به صف برگردانید." },
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
  if (!s.enabled && s.disabled_reason) return { label: "خاموش با ناظر", tone: "warning" };
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

// --- the content supervisor (D52) --------------------------------------------------------------------------

export const CATEGORY_LABEL: Record<string, string> = {
  politics: "سیاسی",
  world: "جهان",
  economy: "اقتصاد",
  society: "اجتماعی",
  incident: "حوادث",
  sport: "ورزش",
  science_tech: "علم و فناوری",
  health: "سلامت",
  culture: "فرهنگ و هنر",
  entertainment: "سرگرمی",
  lifestyle: "سبک زندگی",
  other: "دیگر",
};

export const FLAG_LABEL: Record<string, string> = {
  sexual: "محتوای جنسی یا برهنگی",
  graphic_violence: "خشونت شدید یا صحنه‌ی دلخراش",
  hate: "نفرت یا توهین به یک گروه",
  ads_scam: "تبلیغ، کلاهبرداری، قمار یا تبلیغ کانال دیگر",
  foreign_branding: "واترمارک یا لوگوی کانال دیگر",
  low_quality: "کیفیت پایین یا ناخوانا",
  clickbait: "تیتر فریبنده یا شایعه",
  manipulation: "تلاش برای دستور دادن به ناظر",
};

export const MODE_LABEL: Record<QcMode, string> = { auto: "خودکار", review: "با تأیید من" };

export const BACKEND_LABEL: Record<QcBackend, string> = {
  auto: "اشتراک، و کلید API پشتیبان",
  bridge: "فقط اشتراک Claude",
  api: "فقط کلید API",
  off: "خاموش",
};

export const API_MODEL_LABEL: Record<ApiModel, string> = {
  "claude-sonnet-5-5": "Sonnet 5.5 (دقیق‌تر)",
  "claude-haiku-5-5": "Haiku 5.5 (ارزان‌تر)",
};

/** The supervisor's verdict on one candidate, for its badge. */
export function qcVerdict(c: Candidate): { label: string; tone: Tone } | null {
  const q = c.qc;
  if (!q) return null;
  if (q.decision === null) return q.error ? { label: "ناظر: هنوز بررسی نشد", tone: "warning" } : null;
  const who = q.backend === "rules" ? "قاعده" : "ناظر";
  if (q.decision === "approve") return { label: `${who}: تأیید`, tone: "success" };
  return { label: `${who}: رد`, tone: "danger" };
}

/** The bridge's state in one Persian line and a tone. */
export function bridgeState(view: QcView): { label: string; tone: Tone } {
  if (view.config.backend === "off") return { label: "ناظر خاموش است", tone: "neutral" };
  if (view.config.backend === "api") {
    return view.api_key_set ? { label: "با کلید API", tone: "success" } : { label: "کلید API ذخیره نشده", tone: "danger" };
  }
  const b = view.bridge;
  if (!b) return { label: "هنوز سنجیده نشده", tone: "info" };
  if (!b.ok) return { label: view.bridge_error_fa ?? "پل در دسترس نیست", tone: view.api_key_set ? "warning" : "danger" };
  if (b.logged_in === false) return { label: "ورود اشتراک Claude باطل شده", tone: "danger" };
  if (b.rate_limit?.status === "rejected") return { label: "سقف مصرف اشتراک فعلاً پر است", tone: "warning" };
  return { label: "پل سالم و وارد شده", tone: "success" };
}

/** «۱۷ از ۲۰ مورد آخر رد شد» for a source, or null before the first decision. */
export function qualityText(q: ContentSource["quality"]): string | null {
  if (!q || q.decided === 0) return null;
  return `ناظر: ${formatNumber(q.rejected)} از ${formatNumber(q.decided)} مورد آخر رد شد`;
}

/** Forbidden words: one per line or comma, trimmed, no duplicates, empty lines dropped. */
export function parseWords(raw: string): string[] {
  const out: string[] = [];
  for (const w of raw.split(/[\n,،]/)) {
    const t = w.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

/** What the server accepts as an Anthropic API key (a format check only). */
export const API_KEY_RE = /^sk-ant-[A-Za-z0-9_-]{20,300}$/;

export type QcNumberKey =
  | "bridge_daily_cap"
  | "api_daily_cap"
  | "per_slot"
  | "lead_minutes"
  | "source_window"
  | "source_min"
  | "source_max_reject_pct"
  | "alert_after_hours";

/** The supervisor's numbers as the dashboard edits them (ranges mirror the server's). */
export const QC_NUMBERS: { key: QcNumberKey; label: string; min: number; max: number; hint: string }[] = [
  { key: "bridge_daily_cap", label: "سقف روزانه‌ی اشتراک", min: 0, max: 500, hint: "بررسی با اشتراک Claude در هر ۲۴ ساعت." },
  { key: "api_daily_cap", label: "سقف روزانه‌ی کلید API", min: 0, max: 500, hint: "بررسی با کلید API در هر ۲۴ ساعت." },
  { key: "per_slot", label: "نامزد برای هر پست", min: 1, max: 10, hint: "بهترین‌ها به ترتیب امتیاز؛ بیشتر از این به Claude نمیرود." },
  { key: "lead_minutes", label: "شروع بررسی پیش از پست (دقیقه)", min: 10, max: 180, hint: "خبر تا حد ممکن تازه میماند." },
  { key: "source_window", label: "داوری منبع روی چند مورد آخر", min: 5, max: 100, hint: "" },
  { key: "source_min", label: "کمترین تعداد برای داوری منبع", min: 3, max: 100, hint: "" },
  {
    key: "source_max_reject_pct",
    label: "خاموشی منبع با درصد رد",
    min: 50,
    max: 100,
    hint: "این درصد یا بیشتر رد شود، منبع خاموش میشود.",
  },
  {
    key: "alert_after_hours",
    label: "هشدار به من پس از (ساعت)",
    min: 1,
    max: 48,
    hint: "فقط وقتی Claude یا همه‌ی منبع‌ها از کار افتاده‌اند.",
  },
];

/** A whole number in [min, max] typed in Persian or Latin digits, or an error (Persian). */
export function parseCount(raw: string, min: number, max: number): number | string {
  const latin = raw.trim().replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)));
  if (!/^\d{1,4}$/.test(latin)) return "یک عدد صحیح بنویسید.";
  const n = Number(latin);
  if (n < min || n > max) return `بین ${toFaDigits(min)} و ${toFaDigits(max)}.`;
  return n;
}
