// Plain-Persian words and sentences of the «آمار» page, from the API numbers. Pure functions (tested).
//
// Sizes follow Hetzner: one "ترابایت" is 1024^4 bytes, so a server with 20 TB included traffic shows
// exactly ۲۰ ترابایت.

import type { AnalyticsSummary, AnalyticsUsage, ChannelHeadline, ChannelStatsSnapshot, Signal, SourceStat } from "./analytics";
import { formatDecimal, formatJalali, formatNumber } from "./format";

export const KB = 1024;
export const MB = KB ** 2;
export const GB = KB ** 3;
export const TB = KB ** 4;

/** Bytes -> "۳٫۲ ترابایت" / "۸۵۰ گیگابایت" / "۱۲ مگابایت". */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  const b = Math.max(0, bytes);
  if (b >= TB) return `${formatNumber(b / TB, b >= 100 * TB ? 0 : b >= 10 * TB ? 1 : 2)} ترابایت`;
  if (b >= GB) return `${formatNumber(b / GB, b >= 100 * GB ? 0 : 1)} گیگابایت`;
  if (b >= MB) return `${formatNumber(b / MB, b >= 100 * MB ? 0 : 1)} مگابایت`;
  if (b >= KB) return `${formatNumber(b / KB, 0)} کیلوبایت`;
  return `${formatNumber(b)} بایت`;
}

/** Bytes as a number of TB (charts). */
export const toTb = (bytes: number | null | undefined): number => (bytes ? bytes / TB : 0);

/** Megabytes -> "۳۵ مگابایت" or "۲٫۹ گیگابایت". */
export function formatMb(mb: number | null | undefined): string {
  if (mb === null || mb === undefined || !Number.isFinite(mb)) return "—";
  return formatBytes(mb * MB);
}

/** Bytes per second -> "۱۲ مگابیت بر ثانیه" (network speed is said in bits). */
export function formatBps(bytesPerSecond: number | null | undefined): string {
  if (bytesPerSecond === null || bytesPerSecond === undefined || !Number.isFinite(bytesPerSecond)) return "—";
  const bits = bytesPerSecond * 8;
  if (bits >= 1e9) return `${formatNumber(bits / 1e9, 1)} گیگابیت بر ثانیه`;
  if (bits >= 1e6) return `${formatNumber(bits / 1e6, 1)} مگابیت بر ثانیه`;
  return `${formatNumber(bits / 1e3, 0)} کیلوبیت بر ثانیه`;
}

/** Percent (already 0..100) -> "۸۵٪". */
export function formatPct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${formatNumber(value, digits)}٪`;
}

/** Signed count with Persian digits, the sign kept on the left in RTL: "+۱۲", "−۳", "۰". */
export function formatSigned(n: number): string {
  if (n === 0) return "۰";
  return `⁦${n > 0 ? "+" : "−"}${formatNumber(Math.abs(n))}⁩`;
}

// --- labels ------------------------------------------------------------------------------------------

export function sourceLabel(s: Pick<SourceStat, "kind" | "name">): string {
  switch (s.kind) {
    case "tracking":
      return s.name ?? "لینک ردیابی";
    case "link":
      return s.name ? `لینک «${s.name}»` : "لینک دعوت بی‌نام";
    case "folder":
      return "لینک پوشه";
    default:
      return "مستقیم یا نامعلوم";
  }
}

export const SIGNAL_TEXT: Record<Signal, { label: string; tone: "success" | "warning" | "danger" }> = {
  ok: { label: "عادی", tone: "success" },
  hot: { label: "پرکار", tone: "warning" },
  near_traffic_limit: { label: "نزدیک سقف ترافیک", tone: "danger" },
};

let regionNames: Intl.DisplayNames | null = null;

/** ISO country code -> Persian name ("IR" -> "ایران"); "other" -> "بقیه". */
export function countryName(code: string): string {
  if (code === "other") return "بقیه";
  try {
    regionNames ??= new Intl.DisplayNames(["fa"], { type: "region" });
    return regionNames.of(code) ?? code;
  } catch {
    return code;
  }
}

// Province names as the offline geo database spells them (English) -> Persian.
const PROVINCES: Record<string, string> = {
  tehran: "تهران",
  alborz: "البرز",
  isfahan: "اصفهان",
  esfahan: "اصفهان",
  fars: "فارس",
  "razavi khorasan": "خراسان رضوی",
  "khorasan razavi": "خراسان رضوی",
  "khorasan-e razavi": "خراسان رضوی",
  "east azerbaijan": "آذربایجان شرقی",
  "west azerbaijan": "آذربایجان غربی",
  khuzestan: "خوزستان",
  mazandaran: "مازندران",
  gilan: "گیلان",
  kerman: "کرمان",
  kermanshah: "کرمانشاه",
  qom: "قم",
  qazvin: "قزوین",
  hamadan: "همدان",
  markazi: "مرکزی",
  yazd: "یزد",
  golestan: "گلستان",
  ardabil: "اردبیل",
  zanjan: "زنجان",
  semnan: "سمنان",
  lorestan: "لرستان",
  kurdistan: "کردستان",
  hormozgan: "هرمزگان",
  bushehr: "بوشهر",
  ilam: "ایلام",
  "sistan and baluchestan": "سیستان و بلوچستان",
  "north khorasan": "خراسان شمالی",
  "south khorasan": "خراسان جنوبی",
  "chaharmahal and bakhtiari": "چهارمحال و بختیاری",
  "kohgiluyeh and boyer-ahmad": "کهگیلویه و بویراحمد",
};

export function regionName(name: string): string {
  if (name === "other") return "بقیه";
  return PROVINCES[name.trim().toLowerCase()] ?? name;
}

// Iranian networks by AS number (public registry names; the operator people know).
const ISPS: Record<string, string> = {
  AS197207: "همراه اول",
  AS44244: "ایرانسل",
  AS57218: "رایتل",
  AS58224: "مخابرات ایران",
  AS12880: "ارتباطات زیرساخت",
  AS31549: "شاتل",
  AS16322: "پارس‌آنلاین",
  AS43754: "آسیاتک",
  AS50810: "مبین‌نت",
  AS56402: "های‌وب",
  AS42337: "رسپینا",
};

export function ispName(asn: string): string {
  if (asn === "other") return "بقیه";
  const known = ISPS[asn];
  return known ? `${known} (${asn})` : asn;
}

/** ISO weekday 1..7 (Monday..Sunday) -> Persian name; rows in Persian order start on Saturday. */
export const WEEK_ROWS: { dow: number; label: string }[] = [
  { dow: 6, label: "شنبه" },
  { dow: 7, label: "یکشنبه" },
  { dow: 1, label: "دوشنبه" },
  { dow: 2, label: "سه‌شنبه" },
  { dow: 3, label: "چهارشنبه" },
  { dow: 4, label: "پنجشنبه" },
  { dow: 5, label: "جمعه" },
];

export function channelStatsStatusText(status: string): string {
  switch (status) {
    case "ok":
      return "آمار از تلگرام خوانده شد.";
    case "stats_dc_other":
      return "تلگرام آمار این کانال را روی مرکز داده‌ی دیگری نگه میدارد؛ خواندن آن از آنجا هنوز ساخته نشده است. تعداد عضو و بازدید پست‌ها خوانده میشود.";
    case "stats_unavailable":
      return "تلگرام آمار این کانال را به حساب دوم نشان نمیدهد. حساب دوم باید ادمین کانال باشد و کانال به حداقل عضوی که تلگرام برای آمار میخواهد رسیده باشد.";
    case "channel_not_found":
      return "حساب دوم این کانال را پیدا نکرد. نام کانال در صفحه‌ی تلگرام درست است؟";
    case "flood_wait":
      return "تلگرام گفت کمی صبر کنیم؛ دور بعد دوباره خوانده میشود.";
    case "session_terminated":
      return "نشست حساب دوم در تلگرام بسته شده است. از صفحه‌ی تلگرام دوباره وصلش کنید.";
    default:
      return "خواندن آمار این بار نشد؛ دور بعد (سه ساعت دیگر) دوباره امتحان میشود.";
  }
}

export function channelStatsOutcomeText(outcome: string | undefined): string | null {
  switch (outcome) {
    case undefined:
      return null;
    case "kill_switch":
      return "کلید توقف روشن است؛ حساب دوم (برای آمار کانال) تا خاموش شدن آن کاری نمیکند.";
    case "not_connected":
      return "حساب دوم تلگرام هنوز وصل نیست؛ آمار بازدید و منبع عضو از راه همان حساب خوانده میشود.";
    case "no_channel":
      return "هیچ کانال عمومی‌ای (با نام @) ثبت نشده است.";
    case "ok":
      return null;
    default:
      return "آخرین خواندن آمار کانال کامل نشد؛ دور بعد دوباره امتحان میشود.";
  }
}

// --- sentences ------------------------------------------------------------------------------------------

/** Total period traffic of our live servers against their total included traffic (0..100). */
export function fleetTrafficShare(s: AnalyticsSummary): number | null {
  let used = 0;
  let included = 0;
  for (const srv of s.servers) {
    if (srv.state === "deleted" || srv.period_out_bytes === null || !srv.included_bytes) continue;
    used += srv.period_out_bytes;
    included += srv.included_bytes;
  }
  return included > 0 ? (used * 100) / included : null;
}

export function trafficSentence(s: AnalyticsSummary): string {
  const t = s.traffic;
  if (t.sampling_since === null) {
    return "هنوز هیچ سروری سنجیده نشده است. وقتی اولین node ساخته شود، هر ۱۵ دقیقه ترافیک و CPU آن از هتزنر خوانده میشود.";
  }
  const share = fleetTrafficShare(s);
  let text = `این ماه ${formatBytes(t.month_out_bytes)} به کاربران خدمات دادیم`;
  if (share !== null) text += `؛ ${formatPct(share, share < 10 ? 1 : 0)} سقف ترافیک رایگان سرورها`;
  if (t.avg_day_out_bytes !== null) text += `؛ روزی به‌طور میانگین ${formatBytes(t.avg_day_out_bytes)}`;
  text += ".";
  if (t.last_month_out_bytes > 0) text += ` ماه پیش: ${formatBytes(t.last_month_out_bytes)}.`;
  return text;
}

export function costSentence(s: AnalyticsSummary): string {
  const c = s.cost;
  const total = formatDecimal(c.total_month_to_date_eur);
  if (c.per_tb_eur === null) {
    return `هزینه‌ی سرورها در این ماه تا امروز ${total} یورو است. هنوز ترافیکی نیست که هزینه‌ی هر ترابایت حساب شود.`;
  }
  let text = `هزینه‌ی سرورها در این ماه تا امروز ${total} یورو است؛ یعنی هر ترابایت حدود ${formatDecimal(c.per_tb_eur)} یورو.`;
  if (c.traffic_overage_eur !== "0.00") text += ` ${formatDecimal(c.traffic_overage_eur)} یورو از آن برای ترافیکِ بیشتر از سقف رایگان است.`;
  return text;
}

export function busiestSentence(s: AnalyticsSummary): string | null {
  const live = s.servers.filter((x) => x.state !== "deleted" && x.traffic_pct !== null);
  if (live.length === 0) return null;
  const top = live.reduce((a, b) => ((b.traffic_pct ?? 0) > (a.traffic_pct ?? 0) ? b : a));
  return `پرمصرف‌ترین سرور، ${top.name}، ${formatPct(top.traffic_pct, 0)} ترافیک رایگان این ماهش را مصرف کرده است.`;
}

export function membersSentence(main: ChannelHeadline | null): string {
  if (!main) return "کانال اصلی هنوز در صفحه‌ی تلگرام ثبت نشده است؛ رشد کانال بعد از ثبت آن شمرده میشود.";
  const count = main.members === null ? "" : `کانال اصلی الان ${formatNumber(main.members)} عضو دارد؛ `;
  if (main.tracking_since === null) {
    return `${count}هنوز هیچ ورود یا خروجی ثبت نشده است (از لحظه‌ای که ربات ادمین است شمرده میشود).`;
  }
  return (
    `${count}امروز ${formatSigned(main.net_today)} نفر خالص ` +
    `(${formatNumber(main.joins_today)} ورود، ${formatNumber(main.leaves_today)} خروج)؛ ` +
    `هفت روز اخیر ${formatSigned(main.net_7d)} و سی روز اخیر ${formatSigned(main.net_30d)}.`
  );
}

export function sourcesSentence(top: SourceStat[]): string | null {
  const best = top.find((x) => x.joins > 0);
  if (!best) return null;
  return `بیشترین عضو تازه در ۳۰ روز اخیر از «${sourceLabel(best)}» آمد (${formatNumber(best.joins)} نفر، ${formatNumber(best.leaves)} نفرشان رفته‌اند).`;
}

export function usersSentence(u: AnalyticsUsage): string {
  if (!u.has_data) {
    return "هنوز گزارشی از کاربران پروکسی نرسیده است. این آمار را عامل خود سرورها میسازد (بی ذخیره‌ی IP) و با به‌روزرسانی بعدی عامل می‌آید.";
  }
  const last = u.days.at(-1);
  let text = last ? `در روز ${utcDayLabel(last.day)} ${formatNumber(last.unique_users)} کاربر یکتا از پروکسی‌ها استفاده کردند` : "کاربران پروکسی";
  const totalUsers = u.by_country.reduce((n, b) => n + b.user_days, 0);
  const iran = u.by_country.find((b) => b.key === "IR");
  if (iran && totalUsers > 0) text += `؛ ${formatPct((iran.user_days * 100) / totalUsers)} از ایران`;
  if (u.per_user_mb.p50 !== null) text += `؛ نصف کاربران کمتر از ${formatMb(u.per_user_mb.p50)} در روز مصرف کردند`;
  return `${text}.`;
}

function changePct(now: number | null, prev: number | null): string | null {
  if (now === null || prev === null || prev <= 0) return null;
  const d = ((now - prev) * 100) / prev;
  return `${d >= 0 ? "بیشتر" : "کمتر"} از دوره‌ی قبل (${formatPct(Math.abs(d))})`;
}

export function postsSentence(stats: ChannelStatsSnapshot | null): string | null {
  if (!stats || stats.views_per_post === null) return null;
  let text = `هر پست به‌طور میانگین ${formatNumber(stats.views_per_post)} بازدید و ${formatNumber(stats.shares_per_post ?? 0)} بازنشر داشت`;
  const change = changePct(stats.views_per_post, stats.views_per_post_prev);
  if (change) text += `؛ بازدیدها ${change}`;
  if (stats.notifications_pct !== null) text += `؛ ${formatPct(stats.notifications_pct)} اعضا اعلان کانال را روشن نگه داشته‌اند`;
  return `${text}.`;
}

/** UTC day of the agents' counters "2026-11-04" -> "۱۳ آبان" (noon UTC is the same Jalali day). */
export function utcDayLabel(day: string): string {
  return formatJalali(new Date(`${day}T12:00:00Z`), "day");
}

/** Tehran day "2026-11-04" -> "۱۳ آبان". */
export function dayLabel(day: string): string {
  return formatJalali(new Date(`${day}T12:00:00+03:30`), "day");
}

// Series names in Telegram's own channel graphs -> Persian (unknown names stay as Telegram sent them).
const TG_NAMES: Record<string, string> = {
  followers: "عضو",
  joined: "پیوستند",
  left: "رفتند",
  views: "بازدید",
  shares: "بازنشر",
  muted: "بی‌صدا",
  unmuted: "با صدا",
  url: "لینک",
  channels: "کانال‌ها",
  search: "جستجو",
  groups: "گروه‌ها",
  pm: "پیام خصوصی",
  "private chats": "پیام خصوصی",
  other: "دیگر",
  "similar channels": "کانال‌های مشابه",
  persian: "فارسی",
  english: "انگلیسی",
  russian: "روسی",
  arabic: "عربی",
  turkish: "ترکی",
};

export const tgSeriesName = (name: string): string => TG_NAMES[name.trim().toLowerCase()] ?? name;
