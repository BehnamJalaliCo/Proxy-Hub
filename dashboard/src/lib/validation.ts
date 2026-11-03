// Client-side checks that mirror the server's rules, so the owner sees a clear Persian hint before
// sending. The server is always the authority; these never replace its checks.

import type { FleetSettings } from "./types";

/** Persian (۰-۹) and Arabic-Indic (٠-٩) digits -> ASCII; Persian decimal separator -> "." */
export function normalizeDigits(input: string): string {
  return input
    .replace(/[۰-۹]/g, (c) => String(c.charCodeAt(0) - 0x06f0))
    .replace(/[٠-٩]/g, (c) => String(c.charCodeAt(0) - 0x0660))
    .replace(/٫/g, ".")
    .replace(/[٬\s]/g, "");
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

const ok = <T>(value: T): Parsed<T> => ({ ok: true, value });
const fail = <T>(error: string): Parsed<T> => ({ ok: false, error });

/** Budget cap in euros: empty means "not set" (null). Decimal string with up to 2 decimals, > 0. */
export function parseBudget(raw: string): Parsed<string | null> {
  const s = normalizeDigits(raw);
  if (s === "") return ok(null);
  if (!/^\d{1,7}(\.\d{1,2})?$/.test(s)) return fail("یک عدد مثبت با حداکثر دو رقم اعشار وارد کنید (مثلاً ۱۲۰ یا ۹۹٫۵۰).");
  const [i = "0", f] = s.split(".");
  const intPart = i.replace(/^0+(?=\d)/, "");
  const value = f !== undefined ? `${intPart}.${f}` : intPart;
  if (Number(value) <= 0) return fail("سقف بودجه باید بیشتر از صفر باشد.");
  return ok(value);
}

/** Whole number within [min, max]; empty -> null only when allowEmpty. */
export function parseIntIn(raw: string, min: number, max: number, allowEmpty: boolean): Parsed<number | null> {
  const s = normalizeDigits(raw);
  if (s === "") return allowEmpty ? ok(null) : fail("این مقدار لازم است.");
  if (!/^\d{1,4}$/.test(s)) return fail("فقط عدد صحیح وارد کنید.");
  const n = Number(s);
  if (n < min || n > max) return fail(`عددی بین ${faNum(min)} و ${faNum(max)} وارد کنید.`);
  return ok(n);
}

/** Hetzner server type or location name: lower-case letters, digits, dashes. */
export const SLUG = /^[a-z0-9][a-z0-9-]{1,30}$/;

export function parseSlugList(items: string[], what: string): Parsed<string[]> {
  if (items.length === 0) return fail(`دست‌کم یک ${what} لازم است.`);
  const bad = items.find((x) => !SLUG.test(x));
  if (bad !== undefined) return fail(`«${bad}» درست نیست: فقط حروف کوچک انگلیسی، رقم و خط تیره.`);
  return ok(Array.from(new Set(items)));
}

/** Raw text of each settings field, as the form holds it. */
export interface SettingsDraft {
  dry_run: boolean;
  budget_cap_eur: string;
  node_cap_total: string;
  creations_per_hour_cap: string;
  target_slots: string;
  allowed_server_types: string[];
  locations: string[];
}

export type DraftErrors = Partial<Record<keyof SettingsDraft, string>>;

export function draftFromSettings(s: FleetSettings): SettingsDraft {
  return {
    dry_run: s.dry_run,
    budget_cap_eur: s.budget_cap_eur ?? "",
    node_cap_total: s.node_cap_total === null ? "" : String(s.node_cap_total),
    creations_per_hour_cap: String(s.creations_per_hour_cap),
    target_slots: String(s.target_slots),
    allowed_server_types: [...s.allowed_server_types],
    locations: [...s.locations],
  };
}

/** Validates a draft against FleetSettings limits (settings_schema.py) and merges it into `base`. */
export function settingsFromDraft(
  draft: SettingsDraft,
  base: FleetSettings,
): { settings: FleetSettings; errors: DraftErrors } {
  const errors: DraftErrors = {};
  const budget = parseBudget(draft.budget_cap_eur);
  const nodeCap = parseIntIn(draft.node_cap_total, 0, 200, true);
  const perHour = parseIntIn(draft.creations_per_hour_cap, 0, 20, false);
  const slots = parseIntIn(draft.target_slots, 0, 200, false);
  const types = parseSlugList(draft.allowed_server_types, "نوع سرور");
  const locs = parseSlugList(draft.locations, "location");
  if (!budget.ok) errors.budget_cap_eur = budget.error;
  if (!nodeCap.ok) errors.node_cap_total = nodeCap.error;
  if (!perHour.ok) errors.creations_per_hour_cap = perHour.error;
  if (!slots.ok) errors.target_slots = slots.error;
  if (!types.ok) errors.allowed_server_types = types.error;
  if (!locs.ok) errors.locations = locs.error;
  const settings: FleetSettings = {
    ...base,
    dry_run: draft.dry_run,
    budget_cap_eur: budget.ok ? budget.value : base.budget_cap_eur,
    node_cap_total: nodeCap.ok ? nodeCap.value : base.node_cap_total,
    creations_per_hour_cap: perHour.ok ? (perHour.value ?? base.creations_per_hour_cap) : base.creations_per_hour_cap,
    target_slots: slots.ok ? (slots.value ?? base.target_slots) : base.target_slots,
    allowed_server_types: types.ok ? types.value : base.allowed_server_types,
    locations: locs.ok ? locs.value : base.locations,
  };
  return { settings, errors };
}

/** Same values? (decimal strings compared as written by parseBudget). */
export function sameSettings(a: FleetSettings, b: FleetSettings): boolean {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}

function canonical(s: FleetSettings): unknown[] {
  return [
    s.kill_switch,
    s.dry_run,
    s.budget_cap_eur === null ? null : Number(s.budget_cap_eur),
    s.node_cap_total,
    s.creations_per_hour_cap,
    s.target_slots,
    [...s.allowed_server_types].sort(),
    [...s.locations].sort(),
  ];
}

// --- login / setup -----------------------------------------------------------------------------

/** Server rule (auth team): 3-32 chars of a-z 0-9 . _ - */
export const USERNAME = /^[a-z0-9._-]{3,32}$/;
export const PASSWORD_MIN = 12;

export function usernameError(u: string): string | null {
  if (u === "") return "نام کاربری لازم است.";
  if (!USERNAME.test(u)) return "۳ تا ۳۲ نویسه: فقط حروف کوچک انگلیسی، رقم، نقطه، خط تیره یا زیرخط.";
  return null;
}

export function passwordError(p: string, username: string): string | null {
  if (p.length < PASSWORD_MIN) return `رمز باید دست‌کم ${faNum(PASSWORD_MIN)} نویسه باشد.`;
  if (username.length >= 3 && p.toLowerCase().includes(username.toLowerCase())) return "رمز نباید نام کاربری را در خود داشته باشد.";
  return null;
}

/** 0..4 strength estimate for the meter (length and variety). Only a hint. */
export function passwordScore(p: string): number {
  if (p.length === 0) return 0;
  let variety = 0;
  if (/[a-z]/.test(p)) variety++;
  if (/[A-Z]/.test(p)) variety++;
  if (/\d/.test(p)) variety++;
  if (/[^A-Za-z0-9]/.test(p)) variety++;
  if (p.length < PASSWORD_MIN) return 1;
  if (p.length >= 20 && variety >= 3) return 4;
  if (p.length >= 16 && variety >= 2) return 3;
  return variety >= 3 ? 3 : 2;
}

/** TOTP code from the Authenticator app: six digits (Persian digits and spaces accepted). */
export function normalizeTotp(raw: string): string {
  return normalizeDigits(raw).replace(/\D/g, "").slice(0, 6);
}

export function isTotp(code: string): boolean {
  return /^\d{6}$/.test(code);
}

function faNum(n: number): string {
  return String(n).replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)] ?? d);
}
