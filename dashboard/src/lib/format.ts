// Persian display helpers: Persian digits, money from decimal strings (no float rounding), and
// Jalali dates in the Asia/Tehran time zone.

import { tz } from "@date-fns/tz";
import { format as formatJ, formatDistanceStrict } from "date-fns-jalali";
import { faIR } from "date-fns-jalali/locale";

export const TEHRAN = "Asia/Tehran";
const inTehran = tz(TEHRAN);

const FA_DIGITS = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];

/** "2026 12.5" -> "۲۰۲۶ ۱۲.۵" (only the digits change). */
export function toFaDigits(input: string | number): string {
  return String(input).replace(/[0-9]/g, (d) => FA_DIGITS[Number(d)] ?? d);
}

/** Groups an integer string with the Persian thousands separator. */
function groupThousands(intPart: string): string {
  return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, "٬");
}

/** Integer or decimal number in Persian digits: 12345.6 -> "۱۲٬۳۴۵٫۶". */
export function formatNumber(value: number, maxFractionDigits = 0): string {
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("fa-IR", {
    maximumFractionDigits: maxFractionDigits,
    minimumFractionDigits: 0,
  }).format(value);
}

/** Decimal string from the API ("1234.5") -> "۱٬۲۳۴٫۵۰". Keeps exact digits: no float parsing. */
export function formatDecimal(value: string | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined) return "—";
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!m) return "—";
  const sign = m[1] === "-" ? "−" : "";
  const intPart = (m[2] ?? "0").replace(/^0+(?=\d)/, "");
  let frac = (m[3] ?? "").slice(0, fractionDigits).padEnd(fractionDigits, "0");
  if (fractionDigits === 0) frac = "";
  const text = groupThousands(intPart) + (frac ? `٫${frac}` : "");
  return sign + toFaDigits(text);
}

/** Money in euros, for labels: "۱۲٫۵۰ یورو". */
export function formatEur(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${formatDecimal(value)} یورو`;
}

/** Decimal string -> number, only for proportions (charts, gauges), never for display. */
export function decimalToNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (!/^-?\d+(\.\d+)?$/.test(value.trim())) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function toDate(iso: string | Date): Date | null {
  const d = iso instanceof Date ? iso : new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export type DateStyle = "date" | "datetime" | "time" | "long" | "day" | "short";

const PATTERNS: Record<DateStyle, string> = {
  date: "yyyy/MM/dd",
  datetime: "yyyy/MM/dd، HH:mm",
  time: "HH:mm:ss",
  long: "EEEE d MMMM yyyy، ساعت HH:mm",
  day: "d MMMM",
  short: "d MMM، HH:mm",
};

/** Jalali date in Tehran time with Persian digits: "۱۴۰۵/۰۸/۱۳، ۰۰:۱۵". */
export function formatJalali(iso: string | Date | null | undefined, style: DateStyle = "datetime"): string {
  if (iso === null || iso === undefined) return "—";
  const d = toDate(iso);
  if (!d) return "—";
  return toFaDigits(formatJ(d, PATTERNS[style], { in: inTehran, locale: faIR }));
}

/** Tehran calendar day key, e.g. "1405-08-13", for grouping events by day. */
export function tehranDayKey(iso: string | Date): string {
  const d = toDate(iso);
  if (!d) return "";
  return formatJ(d, "yyyy-MM-dd", { in: inTehran });
}

/** "۵ دقیقه پیش"; "همین حالا" under ten seconds. */
export function formatRelative(iso: string | Date | null | undefined, now: Date = new Date()): string {
  if (iso === null || iso === undefined) return "—";
  const d = toDate(iso);
  if (!d) return "—";
  const diff = now.getTime() - d.getTime();
  if (Math.abs(diff) < 10_000) return "همین حالا";
  const text = formatDistanceStrict(d, now, { locale: faIR, addSuffix: true });
  return toFaDigits(text.replace(/ قبل$/, " پیش"));
}

/** Seconds -> "۴۵ ثانیه" / "۳ دقیقه" / "۲ ساعت و ۵ دقیقه" / "۳ روز". */
export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${formatNumber(s)} ثانیه`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${formatNumber(m)} دقیقه`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 24) return rm ? `${formatNumber(h)} ساعت و ${formatNumber(rm)} دقیقه` : `${formatNumber(h)} ساعت`;
  return `${formatNumber(Math.floor(h / 24))} روز`;
}

/** TOTP secret in groups of four for manual typing: "ABCD EFGH IJKL". */
export function groupSecret(secret: string): string {
  return secret.replace(/\s+/g, "").replace(/(.{4})(?=.)/g, "$1 ");
}

const PERCENT = new Intl.NumberFormat("fa-IR", { style: "percent", maximumFractionDigits: 0 });

/** Ratio as Persian percent: 0.424 -> "۴۲٪". */
export function formatPercent(ratio: number): string {
  if (!Number.isFinite(ratio)) return "—";
  return PERCENT.format(ratio);
}
