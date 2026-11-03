// Telegram page helpers (pure, unit-tested): no-post windows (decision D21) and the post interval,
// mirroring the server's rules (panel telegram/schedule.py), plus channel names, the bot-token
// shape and the pairing countdown. The server is always the authority. No helper here ever puts a
// token into a message it returns.

import { formatDurationSeconds, TEHRAN, toFaDigits } from "./format";
import type { QuietWindow, TgSettings } from "./types";
import { asciiDigits, normalizeDigits, parseIntIn, type Parsed } from "./validation";

export const MAX_WINDOWS = 24;
export const MAX_POST_INTERVAL_MIN = 24 * 60;
const DAY = 24 * 60;
const HHMM = /^([01]\d|2[0-3]):([0-5]\d)$/;

// --- times of day ---------------------------------------------------------------------------------

/** "9:5" is not accepted, "9:05" / "۰۹:۰۵" / "09.05" become "09:05"; anything else null. */
export function normalizeHHMM(raw: string): string | null {
  const s = normalizeDigits(raw).replace(/[.:٫]/g, ":");
  const m = /^(\d{1,2}):(\d{2})$/.exec(s);
  if (!m) return null;
  const v = `${(m[1] ?? "").padStart(2, "0")}:${m[2] ?? ""}`;
  return HHMM.test(v) ? v : null;
}

export function isHHMM(v: string): boolean {
  return HHMM.test(v);
}

/** Minutes after midnight of a valid "HH:MM". */
export function minutesOf(hhmm: string): number {
  const [h = "0", m = "0"] = hhmm.split(":");
  return Number(h) * 60 + Number(m);
}

/** "23:00" -> "۲۳:۰۰" */
export function faTime(hhmm: string): string {
  return toFaDigits(hhmm);
}

// --- no-post windows ------------------------------------------------------------------------------

export function crossesMidnight(w: QuietWindow): boolean {
  return isHHMM(w.start) && isHHMM(w.end) && minutesOf(w.end) < minutesOf(w.start);
}

/** Length in minutes (a window crossing midnight wraps); 0 for an invalid or empty window. */
export function windowLength(w: QuietWindow): number {
  if (!isHHMM(w.start) || !isHHMM(w.end)) return 0;
  return (minutesOf(w.end) - minutesOf(w.start) + DAY) % DAY;
}

export function windowLengthText(w: QuietWindow): string {
  return formatDurationSeconds(windowLength(w) * 60);
}

export function covers(w: QuietWindow, minute: number): boolean {
  if (!isHHMM(w.start) || !isHHMM(w.end)) return false;
  const s = minutesOf(w.start);
  const e = minutesOf(w.end);
  return e < s ? minute >= s || minute < e : minute >= s && minute < e;
}

/** True if together the windows leave no minute of the day for a post (the server refuses it). */
export function coversWholeDay(windows: QuietWindow[]): boolean {
  const covered = new Uint8Array(DAY);
  for (const w of windows) {
    const len = windowLength(w);
    const s = isHHMM(w.start) ? minutesOf(w.start) : 0;
    for (let i = 0; i < len; i++) covered[(s + i) % DAY] = 1;
  }
  return covered.every((c) => c === 1);
}

/** Parts of the day a window covers, as [from, to) minutes, split at midnight (for the timeline). */
export function segments(w: QuietWindow): { from: number; to: number }[] {
  if (windowLength(w) === 0) return [];
  const s = minutesOf(w.start);
  const e = minutesOf(w.end);
  return e > s ? [{ from: s, to: e }] : [{ from: s, to: DAY }, ...(e > 0 ? [{ from: 0, to: e }] : [])];
}

/** Minutes after midnight in Tehran for `now`. */
export function tehranMinutes(now: Date): number {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: TEHRAN, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(now);
  const h = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
  const m = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
  return (h % 24) * 60 + m;
}

/** The window that covers `now` (Tehran time), if any. */
export function activeWindow(windows: QuietWindow[], now: Date): QuietWindow | null {
  const minute = tehranMinutes(now);
  return windows.find((w) => covers(w, minute)) ?? null;
}

export interface ScheduleErrors {
  /** One entry per window: null if that window is fine. */
  windows: (string | null)[];
  interval?: string;
  /** About the windows together (too many, the whole day). */
  form?: string;
}

/** Checks the editor's draft exactly as the server will; `settings` is null while anything fails. */
export function validateSchedule(windows: QuietWindow[], intervalRaw: string): { settings: TgSettings | null; errors: ScheduleErrors } {
  const errors: ScheduleErrors = { windows: [] };
  const clean: QuietWindow[] = [];
  for (const w of windows) {
    const start = normalizeHHMM(w.start);
    const end = normalizeHHMM(w.end);
    if (start === null || end === null) {
      errors.windows.push(`ساعت ${start === null ? "آغاز" : "پایان"} را ۲۴ساعته و به شکل HH:MM وارد کنید (مثلاً ۲۱:۳۰).`);
    } else if (start === end) {
      errors.windows.push("آغاز و پایان یک بازه نباید یکی باشد.");
    } else {
      errors.windows.push(null);
    }
    clean.push({ start: start ?? w.start, end: end ?? w.end });
  }
  if (windows.length > MAX_WINDOWS) errors.form = `حداکثر ${toFaDigits(MAX_WINDOWS)} بازه ممکن است.`;
  else if (errors.windows.every((e) => e === null) && coversWholeDay(clean)) {
    errors.form = "این بازه‌ها کل شبانه‌روز را میپوشانند؛ آن‌وقت هیچ پستی نمیرود.";
  }
  const interval = parseIntIn(intervalRaw, 0, MAX_POST_INTERVAL_MIN, false);
  if (!interval.ok) {
    errors.interval = interval.error;
    return { settings: null, errors };
  }
  const ok = errors.windows.every((e) => e === null) && errors.form === undefined;
  return { settings: ok ? { quiet_windows: clean, post_interval_min: interval.value ?? 0 } : null, errors };
}

export function sameSchedule(a: TgSettings, b: TgSettings): boolean {
  return (
    a.post_interval_min === b.post_interval_min &&
    a.quiet_windows.length === b.quiet_windows.length &&
    a.quiet_windows.every((w, i) => w.start === b.quiet_windows[i]?.start && w.end === b.quiet_windows[i].end)
  );
}

// --- channels ---------------------------------------------------------------------------------------

const USERNAME = /^@?([A-Za-z][A-Za-z0-9_]{3,31})$/;
const PRIVATE_ID = /^-100\d{5,16}$/;

/** "@ipoxxo", "ipoxxo", "https://t.me/ipoxxo" -> "@ipoxxo"; a private channel's id "-100…" as is. */
export function normalizeChannel(raw: string): Parsed<string> {
  let s = raw.trim().replace(/^https?:\/\//i, "").replace(/^(www\.)?(t|telegram)\.me\//i, "").replace(/\/+$/, "");
  if (s === "") return { ok: false, error: "نام کانال را وارد کنید؛ مثلاً ‎@ipoxxo‎." };
  if (s.startsWith("+") || /^joinchat\//i.test(s)) {
    return { ok: false, error: "لینک دعوت کار نمیکند. نام عمومی کانال (‎@name‎) یا شناسه‌ی عددی کانال خصوصی (‎-100…‎) را وارد کنید." };
  }
  s = asciiDigits(s);
  if (PRIVATE_ID.test(s)) return { ok: true, value: s };
  const m = USERNAME.exec(s);
  if (!m) return { ok: false, error: "نام کانال ۵ تا ۳۲ نویسه است: حرف و رقم انگلیسی و زیرخط، با حرف شروع میشود." };
  return { ok: true, value: `@${m[1] ?? ""}` };
}

// --- bot token and pairing ----------------------------------------------------------------------------

const BOT_TOKEN = /^\d{5,16}:[A-Za-z0-9_-]{30,64}$/;

/** A Persian hint if the pasted text cannot be a bot token (null if it looks right). Never echoes it. */
export function botTokenProblem(raw: string): string | null {
  const s = raw.trim();
  if (s === "") return "توکن ربات را از ‎@BotFather‎ بچسبانید.";
  if (!BOT_TOKEN.test(s)) return "این شکل توکن ربات نیست. توکن BotFather یک عدد، دونقطه و یک رشته‌ی بلند انگلیسی است (مثل ‎123456789:AA…‎).";
  return null;
}

const BOT_USERNAME = /^[A-Za-z][A-Za-z0-9_]{3,31}$/;

/** https://t.me/<bot>; null if the name is not a plain bot username (never build odd links). */
export function botLink(username: string): string | null {
  return BOT_USERNAME.test(username) ? `https://t.me/${username}` : null;
}

/** Telegram deep link: opening it and pressing Start sends "/start <code>" to the bot. */
export function pairingLink(username: string, code: string): string | null {
  const base = botLink(username);
  return base && /^[A-Za-z0-9_-]{1,64}$/.test(code) ? `${base}?start=${code}` : null;
}

export function startCommand(code: string): string {
  return `/start ${code}`;
}

export function secondsLeft(expiresAt: string, now: Date = new Date()): number {
  const t = new Date(expiresAt).getTime();
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.floor((t - now.getTime()) / 1000));
}

/** 598 -> "۰۹:۵۸" */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return toFaDigits(`${mm}:${ss}`);
}
