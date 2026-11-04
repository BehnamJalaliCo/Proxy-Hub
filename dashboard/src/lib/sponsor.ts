// Proxy Sponsor and @MTProxybot registrations (docs/11 section 2, decisions D25 and D37): the types,
// API calls, query hook, Persian words and pure helpers of the «اسپانسر» page. Input checks mirror
// the server's (panel/proxyhub/sponsor/channels.py, routes_sponsor.py); the server is the authority.
// No proxy secret ever reaches the browser: tags come shortened, the bot's reply already masked.

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing, request } from "./api";
import { formatDecimal, formatJalali, formatNumber, toFaDigits } from "./format";
import type { Tone } from "./labels";
import { asciiDigits, type Parsed } from "./validation";

// --- types (GET /api/sponsor) ---------------------------------------------------------------------------

export interface SponsorSettings {
  default_channel: string;
  lead_minutes: number;
}

export type RegState = "pending" | "registered" | "failed" | "retired";
export type CampaignState = "draft" | "scheduled" | "active" | "finished" | "cancelled";

export interface SponsorStuck {
  reason: string | null;
  reason_fa: string | null;
  hint: string | null;
  hint_fa: string | null;
  step: string | null;
  step_fa: string | null;
  at: string | null;
  /** The bot's last reply, with every proxy secret already masked by the server. */
  bot_text: string | null;
}

export interface SponsorRegistration {
  id: string;
  address: string;
  host: string;
  port: number;
  node_id: string | null;
  node_state: string | null;
  in_use: boolean;
  state: RegState;
  state_reason: string | null;
  has_tag: boolean;
  tag_hint: string | null;
  sponsor_channel: string | null;
  wanted_channel: string | null;
  in_sync: boolean;
  registered_at: string | null;
  promotion_set_at: string | null;
  visible_from: string | null;
  attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  stuck: SponsorStuck | null;
}

export interface SponsorCampaign {
  id: string;
  customer: string;
  channel: string;
  link: string;
  starts_at: string;
  ends_at: string;
  switch_at: string;
  price: string | null;
  currency: string | null;
  paid: boolean;
  state: CampaignState;
  effective: boolean;
  created_at: string | null;
}

export interface SponsorOverview {
  settings: SponsorSettings;
  kill_switch: boolean;
  provider_ready: boolean;
  account: { state: string; connected: boolean; mtproxybot_ok: boolean };
  paused: boolean;
  stuck_count: number;
  pause_after_stuck: number;
  pacing: { sent_last_hour: number; hourly_cap: number; min_gap_s: number; hold_until: string | null };
  wanted_channel: string;
  active_campaign_id: string | null;
  registrations: SponsorRegistration[];
  campaigns: SponsorCampaign[];
}

export interface CampaignBody {
  customer: string | null;
  channel: string;
  starts_at: string;
  ends_at: string;
  price: string | null;
  currency: string | null;
  paid: boolean;
}

// --- API ---------------------------------------------------------------------------------------------

const BASE = "/api/sponsor";
const id = (value: string) => encodeURIComponent(value);

export const getSponsor = (signal?: AbortSignal) =>
  request<SponsorOverview>("GET", BASE, undefined, signal ? { signal } : {});
export const putSponsorSettings = (body: SponsorSettings) => request<SponsorOverview>("PUT", `${BASE}/settings`, body);
export const retryRegistration = (regId: string) =>
  request<SponsorOverview>("POST", `${BASE}/registrations/${id(regId)}/retry`);
export const createCampaign = (body: CampaignBody) =>
  request<SponsorOverview & { id: string }>("POST", `${BASE}/campaigns`, body);
export const updateCampaign = (campaignId: string, body: CampaignBody) =>
  request<SponsorOverview>("PUT", `${BASE}/campaigns/${id(campaignId)}`, body);
export const cancelCampaign = (campaignId: string) =>
  request<SponsorOverview>("POST", `${BASE}/campaigns/${id(campaignId)}/cancel`);
export const deleteCampaign = (campaignId: string) =>
  request<SponsorOverview>("DELETE", `${BASE}/campaigns/${id(campaignId)}`);

export const sponsorKey = ["sponsor"] as const;

export const useSponsor = () =>
  useQuery({
    queryKey: sponsorKey,
    queryFn: ({ signal }) => getSponsor(signal),
    refetchInterval: (query) => (isEndpointMissing(query.state.error) ? false : 20_000),
  });

// --- words ---------------------------------------------------------------------------------------------

export const REG_STATE: Record<RegState, { label: string; tone: Tone }> = {
  pending: { label: "در انتظار ثبت", tone: "info" },
  registered: { label: "ثبت‌شده", tone: "success" },
  failed: { label: "متوقف: منتظر شما", tone: "danger" },
  retired: { label: "بازنشسته", tone: "neutral" },
};

export function regStateInfo(state: string): { label: string; tone: Tone } {
  return (REG_STATE as Record<string, { label: string; tone: Tone } | undefined>)[state] ?? { label: state, tone: "neutral" };
}

export const CAMPAIGN_STATE: Record<CampaignState, { label: string; tone: Tone }> = {
  draft: { label: "پیش‌نویس", tone: "neutral" },
  scheduled: { label: "زمان‌بندی‌شده", tone: "info" },
  active: { label: "در حال اجرا", tone: "success" },
  finished: { label: "تمام‌شده", tone: "neutral" },
  cancelled: { label: "لغو شده", tone: "warning" },
};

export function campaignStateInfo(state: string): { label: string; tone: Tone } {
  return (CAMPAIGN_STATE as Record<string, { label: string; tone: Tone } | undefined>)[state] ?? { label: state, tone: "neutral" };
}

/** Why the last conversation did not finish (transient: the panel tries again by itself). */
export function lastErrorText(code: string | null | undefined): string | null {
  switch (code) {
    case null:
    case undefined:
      return null;
    case "telegram_unavailable":
      return "به تلگرام نرسید؛ خودکار دوباره امتحان میشود.";
    case "flood_wait":
      return "تلگرام خواست صبر کنیم؛ تا آن زمان هیچ پیامی فرستاده نمیشود.";
    case "hold":
      return "منتظر پایان مهلتی که تلگرام خواسته است.";
    case "no_reply":
      return "‎@MTProxybot‎ جواب نداد؛ دوباره امتحان میشود. سه بار پشت سر هم یعنی توقف و خبر به شما.";
    case "hourly_cap":
      return "سقف ۳۰ پیام در ساعت پر بود؛ ساعت بعد ادامه میدهد.";
    case "kill_switch":
      return "کلید توقف روشن شد و گفتگو نیمه‌کاره ماند.";
    case "session_terminated":
      return "نشست حساب عملیاتی بسته شده است؛ از صفحه‌ی تلگرام دوباره وارد شوید.";
    case "pace_timeout":
    case "too_many_steps":
      return "گفتگو کامل نشد؛ دوباره امتحان میشود.";
    default:
      return `گفتگو کامل نشد (${code}).`;
  }
}

/** "@name" kept left-to-right inside Persian text (left-to-right marks around it). */
export function channelText(username: string | null | undefined): string {
  return username ? `\u200e@${username}\u200e` : "—";
}

// --- the module's state ----------------------------------------------------------------------------------

export interface ModuleStatus {
  tone: Tone;
  title: string;
  text: string;
  /** Where the owner can fix it, if anywhere. */
  link?: { to: string; label: string };
}

export function moduleStatus(o: SponsorOverview, now: Date = new Date()): ModuleStatus {
  if (o.kill_switch) {
    return {
      tone: "danger",
      title: "متوقف: کلید توقف روشن است",
      text: "تا کلید توقف خاموش نشود، پنل با ‎@MTProxybot‎ گفتگو نمیکند و کمپین‌ها هم جلو نمیروند.",
      link: { to: "/settings", label: "تنظیمات ناوگان" },
    };
  }
  if (!o.provider_ready) {
    return {
      tone: "info",
      title: "منتظر فهرست پروکسی‌های MTProto",
      text: "بخش پروکسی‌های MTProto با به‌روزرسانی بعدی پنل وصل میشود؛ تا آن وقت فقط کمپین‌ها زمان‌بندی میشوند.",
    };
  }
  if (!o.account.connected) {
    return {
      tone: "warning",
      title: "حساب عملیاتی وصل نیست",
      text: "ثبت در ‎@MTProxybot‎ با حساب دوم تلگرام انجام میشود. حساب را در صفحه‌ی تلگرام وصل کنید.",
      link: { to: "/telegram", label: "صفحه‌ی تلگرام" },
    };
  }
  if (!o.account.mtproxybot_ok) {
    return {
      tone: "warning",
      title: "‎@MTProxybot‎ هنوز پیدا نشده",
      text: "حساب عملیاتی وصل است ولی ‎@MTProxybot‎ را هنوز پیدا نکرده؛ بررسی خودکار حساب (هر ۳۰ دقیقه) دوباره امتحان میکند.",
      link: { to: "/telegram", label: "صفحه‌ی تلگرام" },
    };
  }
  if (o.paused) {
    return {
      tone: "danger",
      title: "گفتگو با ‎@MTProxybot‎ متوقف است",
      text: `${toFaDigits(o.stuck_count)} ثبت به پاسخی خورده که پنل نمیشناسد. پس از بررسی پاسخ ربات، «ثبت دوباره» را بزنید.`,
    };
  }
  if (o.pacing.hold_until !== null && new Date(o.pacing.hold_until) > now) {
    return {
      tone: "warning",
      title: "تلگرام خواسته صبر کنیم",
      text: `تا ${formatJalali(o.pacing.hold_until, "short")} هیچ پیامی به ‎@MTProxybot‎ فرستاده نمیشود؛ بعد خودکار ادامه میدهد.`,
    };
  }
  return {
    tone: "success",
    title: "خودکار کار میکند",
    text: "هر پروکسی تازه خودکار ثبت میشود و اسپانسرش طبق برنامه‌ی کمپین‌ها تنظیم میشود.",
  };
}

export interface RegCounts {
  live: number;
  inSync: number;
  pending: number;
  failed: number;
}

export function regCounts(regs: SponsorRegistration[]): RegCounts {
  const live = regs.filter((r) => r.state !== "retired");
  return {
    live: live.length,
    inSync: live.filter((r) => r.in_sync).length,
    pending: live.filter((r) => r.state === "pending" || (r.state === "registered" && !r.in_sync)).length,
    failed: live.filter((r) => r.state === "failed").length,
  };
}

/** Live first (stuck ones on top), retired last; then by address. */
export function sortRegistrations(regs: SponsorRegistration[]): SponsorRegistration[] {
  const rank: Record<string, number | undefined> = { failed: 0, pending: 1, registered: 2, retired: 3 };
  return [...regs].sort(
    (a, b) => (rank[a.state] ?? 9) - (rank[b.state] ?? 9) || a.address.localeCompare(b.address, "en"),
  );
}

/** One line about a registration's sponsor. */
export function sponsorLine(r: SponsorRegistration, now: Date = new Date()): { tone: Tone; text: string } {
  if (r.state === "retired") return { tone: "neutral", text: "این ثبت دیگر استفاده نمیشود." };
  if (!r.has_tag) return { tone: "info", text: "هنوز tag ندارد؛ اول در ‎@MTProxybot‎ ثبت میشود." };
  if (r.in_sync) {
    if (r.visible_from !== null && new Date(r.visible_from) > now) {
      return {
        tone: "info",
        text: `اسپانسر ${channelText(r.sponsor_channel)} تنظیم شد؛ حدود ${formatJalali(r.visible_from, "short")} روی تلگرام دیده میشود.`,
      };
    }
    return { tone: "success", text: `اسپانسر: ${channelText(r.sponsor_channel)}` };
  }
  const now_ = r.sponsor_channel ? `فعلاً ${channelText(r.sponsor_channel)}` : "هنوز تنظیم نشده";
  return { tone: "warning", text: `باید ${channelText(r.wanted_channel)} شود (${now_}).` };
}

// --- campaigns ---------------------------------------------------------------------------------------

const NAME = /^[A-Za-z][A-Za-z0-9_]{3,31}$/;
const LINK = /^(?:(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/)?@?([^/?#\s]+)\/?$/i;
const PRIVATE = /(?:t\.me|telegram\.me|telegram\.dog)\/(?:\+|joinchat\/)/i;
const RESERVED = new Set(["proxy", "socks", "share", "addstickers", "addemoji", "joinchat", "setlanguage"]);

/** A public channel link -> its username (lower-case, no @). Private invite links are refused. */
export function parseChannel(raw: string): Parsed<string> {
  const text = raw.trim();
  if (text === "") return { ok: false, error: "لینک کانال عمومی را وارد کنید (مثل ‎https://t.me/name‎ یا ‎@name‎)." };
  if (PRIVATE.test(text) || text.startsWith("+")) {
    return { ok: false, error: "لینک خصوصی پذیرفته نمیشود؛ ‎@MTProxybot‎ فقط کانال عمومی را قبول میکند." };
  }
  const m = LINK.exec(text);
  const name = m?.[1] ?? "";
  if (!NAME.test(name) || name.endsWith("_") || RESERVED.has(name.toLowerCase())) {
    return { ok: false, error: "این شکل لینک کانال عمومی نیست: ‎https://t.me/name‎ یا ‎@name‎ (۴ تا ۳۲ حرف لاتین، رقم یا _)." };
  }
  return { ok: true, value: name.toLowerCase() };
}

const pad = (n: number) => String(n).padStart(2, "0");

/** An ISO time -> the value of an <input type="datetime-local"> in this device's time zone. */
export function toLocalInput(iso: string | Date): string {
  const d = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getFullYear())}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The value of an <input type="datetime-local"> (this device's time zone) -> ISO in UTC, or null. */
export function fromLocalInput(value: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export interface CampaignDraft {
  customer: string;
  channel: string;
  starts: string; // datetime-local
  ends: string; // datetime-local
  price: string;
  currency: string;
  paid: boolean;
}

export type CampaignErrors = Partial<Record<keyof CampaignDraft, string>>;

/** A new campaign: starts in two hours (on the hour), runs for a day. */
export function emptyDraft(now: Date = new Date()): CampaignDraft {
  const start = new Date(now.getTime() + 2 * 3_600_000);
  start.setMinutes(0, 0, 0);
  return {
    customer: "",
    channel: "",
    starts: toLocalInput(start),
    ends: toLocalInput(new Date(start.getTime() + 24 * 3_600_000)),
    price: "",
    currency: "EUR",
    paid: false,
  };
}

export function draftFromCampaign(c: SponsorCampaign): CampaignDraft {
  return {
    customer: c.customer === `@${c.channel}` ? "" : c.customer,
    channel: `https://t.me/${c.channel}`,
    starts: toLocalInput(c.starts_at),
    ends: toLocalInput(c.ends_at),
    price: c.price ?? "",
    currency: c.currency ?? "",
    paid: c.paid,
  };
}

const PRICE = /^\d{1,10}(\.\d{1,2})?$/;
const CURRENCY = /^[A-Z]{3,8}$/;

/** The body to send, or the errors per field (Persian). ``started``: the start can no longer move. */
export function campaignBody(
  d: CampaignDraft,
  opts: { now?: Date; defaultChannel?: string; started?: boolean } = {},
): { ok: true; body: CampaignBody } | { ok: false; errors: CampaignErrors } {
  const now = opts.now ?? new Date();
  const errors: CampaignErrors = {};
  const channel = parseChannel(d.channel);
  if (!channel.ok) errors.channel = channel.error;
  else if (opts.defaultChannel !== undefined && channel.value === opts.defaultChannel) {
    errors.channel = "این همان کانال پیش‌فرض اسپانسر است؛ کمپین برای کانال مشتری است.";
  }
  const starts = fromLocalInput(d.starts);
  const ends = fromLocalInput(d.ends);
  if (starts === null) errors.starts = "زمان شروع را کامل وارد کنید.";
  else if (!opts.started && new Date(starts).getTime() < now.getTime() - 5 * 60_000) {
    errors.starts = "شروع کمپین گذشته است.";
  }
  if (ends === null) errors.ends = "زمان پایان را کامل وارد کنید.";
  else if (starts !== null && new Date(ends) <= new Date(starts)) errors.ends = "پایان باید بعد از شروع باشد.";
  else if (new Date(ends) <= now) errors.ends = "پایان کمپین گذشته است.";
  const price = asciiDigits(d.price.trim()).replace(/[٫,]/g, ".");
  if (price !== "" && !PRICE.test(price)) errors.price = "قیمت را عددی مثبت با حداکثر دو رقم اعشار وارد کنید.";
  const currency = d.currency.trim().toUpperCase();
  if (currency !== "" && !CURRENCY.test(currency)) errors.currency = "۳ تا ۸ حرف لاتین، مثل EUR یا USDT.";
  const customer = d.customer.trim();
  if (customer.length > 200) errors.customer = "نام مشتری حداکثر ۲۰۰ نویسه است.";
  if (Object.keys(errors).length > 0 || !channel.ok || starts === null || ends === null) return { ok: false, errors };
  return {
    ok: true,
    body: {
      customer: customer === "" ? null : customer,
      channel: `https://t.me/${channel.value}`,
      starts_at: starts,
      ends_at: ends,
      price: price === "" ? null : price,
      currency: currency === "" ? null : currency,
      paid: d.paid,
    },
  };
}

export const canEdit = (c: SponsorCampaign) => c.state === "scheduled" || c.state === "active";
export const canCancel = canEdit;
/** Only a campaign whose switch has not begun may be deleted; one that ran stays on record. */
export const canDelete = (c: SponsorCampaign, now: Date = new Date()) =>
  c.state === "scheduled" && now < new Date(c.switch_at);

export function windowText(c: SponsorCampaign): string {
  return `${formatJalali(c.starts_at, "short")} تا ${formatJalali(c.ends_at, "short")}`;
}

export function priceText(price: string | null, currency: string | null): string {
  if (price === null) return "بی قیمت";
  return `${formatDecimal(price)} ${currency ?? ""}`.trim();
}

export function leadText(minutes: number): string {
  if (minutes === 0) return "درست در زمان شروع";
  if (minutes % 60 === 0) return `${formatNumber(minutes / 60)} ساعت زودتر`;
  return `${formatNumber(minutes)} دقیقه زودتر`;
}
