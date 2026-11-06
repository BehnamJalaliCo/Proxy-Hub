// Automatic posts (team posts): types and endpoints of /api/posts/*, the Persian words of their states,
// checks that mirror the server's (panel/proxyhub/posts/settings.py, posts/content.py) and a small
// reader for the Telegram HTML of a post, so a preview is drawn from React elements and never through
// innerHTML. The server is always the authority.

import { request } from "./api";
import type { Tone } from "./labels";
import { minutesOf, normalizeHHMM } from "./telegram";
import { asciiDigits } from "./validation";

// --- types ------------------------------------------------------------------------------------------

export type PostRole = "main" | "test";
export type ContentStatus = "draft" | "approved" | "used" | "retired";
export type LinkKind = "mtproto" | "reality";
export type LinkStatus = "active" | "expired" | "full" | "burned";
export type PostState = "queued" | "sent" | "failed" | "unknown";

export interface StatusTexts {
  expired: string;
  full: string;
  burned: string;
}

export interface PostingSettings {
  enabled: boolean;
  main_enabled: boolean;
  posts_per_day: number;
  day_start: string;
  day_end: string;
  max_mtproto: number;
  max_reality: number;
  edit_window_hours: number;
  recycle_after_days: number;
  status_texts: StatusTexts;
}

export interface PlanSlot {
  at: string;
  day: string;
  index: number;
}

export interface PostsStatus {
  target: PostRole;
  blocked: string | null;
  channels: Record<PostRole, string | null>;
  channel_tag: string;
  quiet_now: boolean;
  post_interval_min: number;
  today: { planned: PlanSlot[]; posted: number };
  next_slot: PlanSlot | null;
  last_post_at: string | null;
  links: { providers: number; complete: boolean; mtproto: number; reality: number };
  content: Record<ContentStatus, number>;
}

export interface PostPreview {
  target: PostRole;
  blocked: string | null;
  text: string;
  chars: number;
  /** Which emoji the second account's post would show animated (D38); older panels omit it. */
  emoji?: { emoji: string; animated: boolean }[];
  content_item: { id: number; title: string; recycled: boolean } | null;
  links: { mtproto: number; reality: number };
}

export interface CarriedLink {
  kind: LinkKind;
  label: string;
  status: LinkStatus;
  status_at: string | null;
}

export interface RecentPost {
  id: number;
  role: PostRole;
  state: PostState;
  created_at: string;
  planned_at: string;
  title: string | null;
  links: CarriedLink[];
  edits: number;
  last_edit_at: string | null;
  edits_stopped: boolean;
  watching: boolean;
  message_url: string | null;
  /** Who posted it (and edits it): the bot or the second account (D38); older panels omit it. */
  poster?: "bot" | "operator";
}

export interface ContentItem {
  id: number;
  title: string;
  body: string;
  tags: string[];
  status: ContentStatus;
  origin: "seed" | "owner";
  used_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ContentList {
  items: ContentItem[];
  counts: Record<ContentStatus, number>;
}

export interface ContentInput {
  title: string;
  body: string;
  tags: string[];
}

// --- endpoints --------------------------------------------------------------------------------------

const opts = (signal?: AbortSignal) => (signal ? { signal } : {});

export const getPostsStatus = (signal?: AbortSignal) => request<PostsStatus>("GET", "/api/posts/status", undefined, opts(signal));
export const getPostPreview = (signal?: AbortSignal) => request<PostPreview>("GET", "/api/posts/preview", undefined, opts(signal));
export const getRecentPosts = (signal?: AbortSignal) =>
  request<{ posts: RecentPost[] }>("GET", "/api/posts/recent?limit=20", undefined, opts(signal));
export const getPostingSettings = (signal?: AbortSignal) =>
  request<PostingSettings>("GET", "/api/posts/settings", undefined, opts(signal));
export const putPostingSettings = (s: PostingSettings) => request<PostingSettings>("PUT", "/api/posts/settings", s);
export const getContent = (signal?: AbortSignal) => request<ContentList>("GET", "/api/posts/content", undefined, opts(signal));
export const createContent = (input: ContentInput, status: "draft" | "approved") =>
  request<ContentItem>("POST", "/api/posts/content", { ...input, status });
export const updateContent = (id: number, input: ContentInput) =>
  request<ContentItem>("PUT", `/api/posts/content/${String(id)}`, input);
export const setContentStatus = (id: number, status: "draft" | "approved" | "retired") =>
  request<ContentItem>("POST", `/api/posts/content/${String(id)}/status`, { status });

// --- words ------------------------------------------------------------------------------------------

export const ROLE_LABEL: Record<PostRole, string> = { main: "کانال اصلی", test: "کانال آزمایشی" };

export const CONTENT_STATUS: Record<ContentStatus, { label: string; tone: Tone; hint: string }> = {
  approved: { label: "تأییدشده", tone: "success", hint: "در صف پست؛ به ترتیب میرود." },
  draft: { label: "پیش‌نویس", tone: "neutral", hint: "تا تأیید نکنید پست نمیشود." },
  used: { label: "استفاده‌شده", tone: "info", hint: "یک بار رفته؛ وقتی محتوای تازه نماند، دوباره میآید." },
  retired: { label: "بازنشسته", tone: "warning", hint: "دیگر پست نمیشود؛ پاک نمیشود." },
};

export const POST_STATE: Record<PostState, { label: string; tone: Tone }> = {
  queued: { label: "در صف ارسال", tone: "info" },
  sent: { label: "فرستاده شد", tone: "success" },
  failed: { label: "نرفت", tone: "danger" },
  unknown: { label: "نامعلوم", tone: "neutral" },
};

export const LINK_STATUS: Record<LinkStatus, { label: string; mark: string; tone: Tone }> = {
  active: { label: "فعال", mark: "", tone: "success" },
  expired: { label: "زمانش تمام شد", mark: "⌛️", tone: "warning" },
  full: { label: "ظرفیتش پر شد", mark: "🔴", tone: "danger" },
  burned: { label: "از دسترس خارج شد", mark: "🚫", tone: "danger" },
};

/** Why the publisher cannot post right now (``blocked`` of status and preview). */
export function blockedText(code: string | null): string | null {
  switch (code) {
    case null:
      return null;
    case "disabled":
      return "انتشار خودکار خاموش است.";
    case "no_bot":
      return "ربات تلگرام هنوز ثبت نشده؛ از صفحه‌ی تلگرام توکن ربات را بدهید.";
    case "no_channel":
      return "کانال مقصد ثبت نشده؛ از صفحه‌ی تلگرام کانال را ثبت کنید.";
    case "no_links":
      return "هیچ پروکسی یا کانفیگ سالمی نیست؛ پست بی پروکسی سالم هرگز نمیرود.";
    default:
      return "پست تازه فعلاً نمیرود.";
  }
}

export function contentStatusInfo(status: string): { label: string; tone: Tone; hint: string } {
  return (CONTENT_STATUS as Record<string, { label: string; tone: Tone; hint: string } | undefined>)[status] ?? { label: status, tone: "neutral", hint: "" };
}

// --- checks (mirror the server) ---------------------------------------------------------------------

export const LIMITS = {
  postsPerDay: [0, 48],
  maxMtproto: [0, 20],
  maxReality: [0, 5],
  editWindowHours: [1, 720],
  recycleAfterDays: [1, 365],
  statusText: 80,
  title: 120,
  bodyVisible: 1500,
  bodyHtml: 3000,
  tags: 8,
  tag: 32,
} as const;

export type SettingsDraft = Omit<PostingSettings, "posts_per_day" | "max_mtproto" | "max_reality" | "edit_window_hours" | "recycle_after_days"> & {
  posts_per_day: string;
  max_mtproto: string;
  max_reality: string;
  edit_window_hours: string;
  recycle_after_days: string;
};

export function toDraft(s: PostingSettings): SettingsDraft {
  return {
    ...s,
    status_texts: { ...s.status_texts },
    posts_per_day: String(s.posts_per_day),
    max_mtproto: String(s.max_mtproto),
    max_reality: String(s.max_reality),
    edit_window_hours: String(s.edit_window_hours),
    recycle_after_days: String(s.recycle_after_days),
  };
}

function intIn(raw: string, [lo, hi]: readonly [number, number]): number | null {
  const s = asciiDigits(raw.trim());
  if (!/^\d{1,4}$/.test(s)) return null;
  const n = Number(s);
  return n >= lo && n <= hi ? n : null;
}

export type SettingsErrors = Partial<Record<keyof PostingSettings | "expired" | "full" | "burned" | "form", string>>;

/** The settings to send, or Persian errors per field. */
export function validateSettings(d: SettingsDraft): { settings: PostingSettings | null; errors: SettingsErrors } {
  const errors: SettingsErrors = {};
  const num = (key: "posts_per_day" | "max_mtproto" | "max_reality" | "edit_window_hours" | "recycle_after_days", range: readonly [number, number]) => {
    const v = intIn(d[key], range);
    if (v === null) errors[key] = `عددی از ${toFa(range[0])} تا ${toFa(range[1])}.`;
    return v ?? 0;
  };
  const postsPerDay = num("posts_per_day", LIMITS.postsPerDay);
  const maxMtproto = num("max_mtproto", LIMITS.maxMtproto);
  const maxReality = num("max_reality", LIMITS.maxReality);
  const editWindow = num("edit_window_hours", LIMITS.editWindowHours);
  const recycle = num("recycle_after_days", LIMITS.recycleAfterDays);
  const start = normalizeHHMM(d.day_start);
  const end = normalizeHHMM(d.day_end);
  if (start === null) errors.day_start = "ساعت به شکل ۰۸:۰۰.";
  if (end === null) errors.day_end = "ساعت به شکل ۲۳:۳۰ (۰۰:۰۰ یعنی نیمه‌شب).";
  if (start !== null && start === end) errors.day_end = "آغاز و پایان ساعت‌های پست نباید یکی باشد.";
  if (!errors.max_mtproto && !errors.max_reality && maxMtproto === 0 && maxReality === 0) {
    errors.max_reality = "دست‌کم یک پروکسی یا یک کانفیگ در هر پست لازم است.";
  }
  const texts = { ...d.status_texts };
  for (const k of ["expired", "full", "burned"] as const) {
    const t = texts[k].replace(/\s+/g, " ").trim();
    texts[k] = t;
    if (t === "") errors[k] = "این متن خالی نمیماند.";
    else if (t.length > LIMITS.statusText) errors[k] = `حداکثر ${toFa(LIMITS.statusText)} نویسه.`;
  }
  if (Object.keys(errors).length > 0) return { settings: null, errors };
  return {
    settings: {
      enabled: d.enabled,
      main_enabled: d.main_enabled,
      posts_per_day: postsPerDay,
      day_start: start ?? "",
      day_end: end ?? "",
      max_mtproto: maxMtproto,
      max_reality: maxReality,
      edit_window_hours: editWindow,
      recycle_after_days: recycle,
      status_texts: texts,
    },
    errors,
  };
}

/** About how many posts the posting hours hold when two posts must be `gapMin` minutes apart
 * (telegram.posting). The publisher posts a slot only within its 45-minute grace, so slots beyond
 * this number are skipped. null: no limit (no gap) or the hours are not valid. */
export function postsThatFit(dayStart: string, dayEnd: string, gapMin: number): number | null {
  const start = normalizeHHMM(dayStart);
  const end = normalizeHHMM(dayEnd);
  if (start === null || end === null || start === end || !(gapMin > 0)) return null;
  const a = minutesOf(start);
  const b = minutesOf(end);
  const span = b > a ? b - a : b + 24 * 60 - a;
  return Math.max(1, Math.floor(span / gapMin));
}

export function sameSettings(a: PostingSettings, b: PostingSettings): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function toFa(n: number): string {
  return String(n).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)] ?? d);
}

/** "ترفند، امنیت #تلگرام" -> ["ترفند", "امنیت", "تلگرام"], or a Persian error. */
export function parseTags(raw: string): { tags: string[]; error: string | null } {
  const parts = raw
    .split(/[,،\s]+/)
    .map((p) => p.trim().replace(/^#+/, ""))
    .filter((p) => p !== "");
  const tags: string[] = [];
  for (const p of parts) {
    const t = /^[ -~]+$/.test(p) ? p.toLowerCase() : p;
    if (!/^[\p{L}\p{N}_‌-]{1,32}$/u.test(t)) return { tags: [], error: "هر برچسب یک کلمه است، با حرف، رقم، - یا _ (حداکثر ۳۲ نویسه)." };
    if (!tags.includes(t)) tags.push(t);
  }
  if (tags.length > LIMITS.tags) return { tags: [], error: `حداکثر ${toFa(LIMITS.tags)} برچسب.` };
  return { tags, error: null };
}

export interface ContentErrors {
  title?: string;
  body?: string;
  tags?: string;
}

export function validateContent(title: string, body: string, tagsRaw: string): { input: ContentInput | null; errors: ContentErrors } {
  const errors: ContentErrors = {};
  const t = title.replace(/\s+/g, " ").trim();
  if (t === "") errors.title = "عنوان لازم است.";
  else if (t.length > LIMITS.title) errors.title = `عنوان حداکثر ${toFa(LIMITS.title)} نویسه است.`;
  const b = body.replace(/\r\n?/g, "\n").trim();
  const shown = visibleLength(b);
  if (shown === 0) errors.body = "متن لازم است.";
  else if (shown > LIMITS.bodyVisible) errors.body = `متن حداکثر ${toFa(LIMITS.bodyVisible)} نویسه است (الان ${toFa(shown)}).`;
  else if (b.length > LIMITS.bodyHtml) errors.body = "متن با برچسب‌های قالب‌بندی بیش از اندازه بلند است.";
  const tags = parseTags(tagsRaw);
  if (tags.error) errors.tags = tags.error;
  if (Object.keys(errors).length > 0) return { input: null, errors };
  return { input: { title: t, body: b, tags: tags.tags }, errors };
}

// --- Telegram HTML reader (preview only) -----------------------------------------------------------

export type TgNode =
  | { type: "text"; text: string }
  /** ``lang``: a pre block's language (``<pre><code class="language-x">``), shown in its header.
   * ``expandable``: a collapsed quote (``<blockquote expandable>``), as a post's VLESS config sits
   * in one (D48); Telegram shows about three lines of it until it is tapped. */
  | { type: "el"; tag: TgTag; href?: string; lang?: string; expandable?: boolean; children: TgNode[] };

export type TgTag = "b" | "i" | "u" | "s" | "code" | "pre" | "blockquote" | "a" | "spoiler";

const TAG_ALIASES: Record<string, TgTag> = {
  b: "b",
  strong: "b",
  i: "i",
  em: "i",
  u: "u",
  ins: "u",
  s: "s",
  strike: "s",
  del: "s",
  code: "code",
  pre: "pre",
  blockquote: "blockquote",
  a: "a",
  "tg-spoiler": "spoiler",
  span: "spoiler",
};

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", "#39": "'", "#x27": "'" };

/** A numeric reference as browsers read it: no code point, a surrogate or beyond U+10FFFF is U+FFFD
 * (String.fromCodePoint would throw past U+10FFFF and break the live preview). */
function codePoint(n: number): string {
  if (!Number.isInteger(n) || n <= 0 || n > 0x10ffff || (n >= 0xd800 && n <= 0xdfff)) return "�";
  return String.fromCodePoint(n);
}

export function decodeEntities(s: string): string {
  return s.replace(/&(#\d{1,7}|#x[0-9a-f]{1,6}|[a-z]{2,6});/gi, (m, name: string) => {
    const known = ENTITIES[name.toLowerCase()];
    if (known !== undefined) return known;
    if (name.startsWith("#x") || name.startsWith("#X")) return codePoint(parseInt(name.slice(2), 16));
    if (name.startsWith("#")) return codePoint(Number(name.slice(1)));
    return m;
  });
}

const SAFE_HREF = /^(https?:\/\/|tg:\/\/)[^\s"<>]+$/i;
const LANGUAGE = /class\s*=\s*"language-([A-Za-z0-9_+-]{1,30})"/i;

/** The tags Telegram shows, as a tree. Unknown tags keep their text; only http(s)/tg links are kept.
 * A ``code`` right inside a ``pre`` is no node of its own: it names the pre block's language, as in
 * Telegram. A ``blockquote expandable`` is a collapsed quote (a post's VLESS config since D48). */
export function parseTgHtml(html: string): TgNode[] {
  const root: TgNode[] = [];
  const stack: { tag: string; node: Extract<TgNode, { type: "el" }> | null }[] = [];
  const current = (): TgNode[] => {
    for (let i = stack.length - 1; i >= 0; i--) {
      const n = stack[i]?.node;
      if (n) return n.children;
    }
    return root;
  };
  const re = /<(\/?)([a-z][a-z0-9-]*)((?:\s+[a-z-]+(?:\s*=\s*"[^"]*")?)*)\s*\/?>|([^<]+)|(<)/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) {
    const [, closing, rawName, attrs, text, lone] = m;
    if (text !== undefined || lone !== undefined) {
      current().push({ type: "text", text: decodeEntities(text ?? lone ?? "") });
      continue;
    }
    const name = (rawName ?? "").toLowerCase();
    if (name === "br") {
      current().push({ type: "text", text: "\n" });
      continue;
    }
    if (closing) {
      const at = stack.map((s) => s.tag).lastIndexOf(name);
      if (at >= 0) stack.length = at;
      continue;
    }
    const tag = TAG_ALIASES[name];
    let node: Extract<TgNode, { type: "el" }> | null = null;
    const pre = stack.at(-1)?.node;
    if (tag === "code" && pre?.tag === "pre") {
      const lang = LANGUAGE.exec(attrs ?? "")?.[1];
      if (lang && pre.lang === undefined) pre.lang = lang;
    } else if (tag && !(name === "span" && !/class\s*=\s*"tg-spoiler"/i.test(attrs ?? ""))) {
      node = { type: "el", tag, children: [] };
      if (tag === "blockquote" && /(^|\s)expandable(\s|=|$)/i.test(attrs ?? "")) node.expandable = true;
      if (tag === "a") {
        const href = decodeEntities(/href\s*=\s*"([^"]*)"/i.exec(attrs ?? "")?.[1] ?? "");
        if (SAFE_HREF.test(href)) node.href = href;
      }
      current().push(node);
    }
    stack.push({ tag: name, node });
  }
  return root;
}

export function plainText(nodes: TgNode[]): string {
  return nodes.map((n) => (n.type === "text" ? n.text : plainText(n.children))).join("");
}

/** What Telegram counts (UTF-16 units of the visible text). */
export function visibleLength(html: string): number {
  return plainText(parseTgHtml(html)).trim().length;
}

/** The emoji the panel puts on a post's title line when the title brings none (D48). */
export const TITLE_MARK = "📡";

/** A post's title line as the panel composes it (panel/proxyhub/telegram/templates.py title_line):
 * one line, with the title's own emoji, else {@link TITLE_MARK} in front of it. */
export function titleLine(title: string): string {
  const t = title.replace(/\s+/g, " ").trim();
  if (t === "" || /\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3/u.test(t)) return t;
  return `${TITLE_MARK} ${t}`;
}

/** One-line excerpt of a body for lists. */
export function excerpt(html: string, max = 140): string {
  const s = plainText(parseTgHtml(html)).replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s;
}

/** Wrap the selected text of a textarea in a tag pair (formatting toolbar). */
export function wrapSelection(
  text: string,
  start: number,
  end: number,
  open: string,
  close: string,
): { text: string; start: number; end: number } {
  const a = Math.max(0, Math.min(start, end, text.length));
  const b = Math.min(text.length, Math.max(start, end));
  const inner = text.slice(a, b);
  return { text: text.slice(0, a) + open + inner + close + text.slice(b), start: a + open.length, end: a + open.length + inner.length };
}
