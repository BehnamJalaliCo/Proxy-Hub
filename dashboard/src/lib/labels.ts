// Persian labels for machine codes the API returns (states, reasons, actions). Unknown codes are
// shown as they are, so a new code on the server is never hidden.

import type { AccountState, FleetSettings, PlanActionKind } from "./types";

export type Tone = "neutral" | "brand" | "success" | "warning" | "danger" | "info";

export const ACCOUNT_STATE: Record<AccountState, { label: string; tone: Tone; hint: string }> = {
  verifying: { label: "در حال وارسی", tone: "info", hint: "توکن و project در حال وارسی‌اند." },
  preparing: { label: "در حال آماده‌سازی", tone: "info", hint: "فایروال و image در این حساب ساخته میشود." },
  active: { label: "فعال", tone: "success", hint: "nodeهای تازه در این حساب ساخته میشوند." },
  draining: { label: "در حال تخلیه", tone: "warning", hint: "node تازه ساخته نمیشود؛ nodeهای موجود بازنشسته میشوند." },
  retired: { label: "بازنشسته", tone: "neutral", hint: "حساب دیگر استفاده نمیشود و میتوانید آن را ببندید." },
  locked: { label: "قفل", tone: "danger", hint: "پنل این حساب را قفل کرده و هیچ کاری در آن نمیکند." },
};

export function accountStateInfo(state: string): { label: string; tone: Tone; hint: string } {
  return (ACCOUNT_STATE as Record<string, { label: string; tone: Tone; hint: string } | undefined>)[state] ?? {
    label: state,
    tone: "neutral",
    hint: "",
  };
}

/** Which owner buttons make sense in each state (the server still decides). */
export function accountActions(state: string): { drain: boolean; retire: boolean } {
  return { drain: state === "active", retire: state === "draining" || state === "locked" };
}

const STATE_REASON: Record<string, string> = {
  verified: "توکن وارسی شد و project فقط سرورهای این سیستم را دارد.",
  foreign_servers: "در این project سروری هست که مال این سیستم نیست؛ حساب قفل شد (قانون ۴).",
  token_rejected: "هتزنر توکن این حساب را دیگر نمیپذیرد (401)؛ حساب قفل شد.",
  token_forbidden: "هتزنر دسترسی این توکن را رد کرد (403)؛ حساب قفل شد.",
  drained_by_owner: "مالک حساب را تخلیه کرد؛ node تازه در آن ساخته نمیشود.",
  retired_by_owner: "حساب بازنشسته شد و توکنش از گاوصندوق پاک شد.",
};

/** "foreign_servers:2" -> Persian sentence (+ the count when there is one). */
export function stateReasonText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const [code = "", detail] = reason.split(":", 2);
  const text = STATE_REASON[code];
  if (!text) return reason;
  return detail && /^\d+$/.test(detail) && code === "foreign_servers" ? `${text} (${toFa(detail)} سرور بیگانه)` : text;
}

function toFa(s: string): string {
  return s.replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)] ?? d);
}

export const SETTINGS_LABEL: Record<keyof FleetSettings, string> = {
  kill_switch: "کلید توقف",
  dry_run: "حالت خشک",
  budget_cap_eur: "سقف بودجه‌ی ماه",
  node_cap_total: "سقف کل node",
  creations_per_hour_cap: "سقف ساخت در ساعت",
  target_slots: "جایگاه‌های هدف",
  allowed_server_types: "نوع‌های مجاز سرور",
  locations: "locationها",
};

const REASON: Record<string, string> = {
  // safety gates (proxyhub/safety/gates.py)
  kill_switch_on: "کلید توقف روشن است",
  budget_cap_not_set: "سقف بودجه تعیین نشده",
  node_cap_not_set: "سقف تعداد node تعیین نشده",
  node_cap_reached: "به سقف تعداد node رسیده",
  budget_cap_reached: "به سقف بودجه‌ی ماه میرسد",
  creation_rate_cap_reached: "به سقف ساخت در ساعت رسیده",
  account_not_usable: "حساب آماده‌ی کار نیست",
  account_guard_failed: "نگهبان حساب سبز نیست",
  account_server_limit_reached: "به سقف سرور این حساب رسیده",
  invalid_cost_input: "داده‌ی هزینه ناقص است",
  label_managed_by_missing: "برچسب managed-by=proxy-hub روی سرور نیست",
  role_not_deletable: "نقش این سرور حذف‌شدنی نیست",
  no_db_row: "سرور در دیتابیس پنل ثبت نشده",
  db_row_hetzner_id_mismatch: "شناسه‌ی هتزنر با دیتابیس نمیخواند",
  db_row_account_mismatch: "حساب سرور با دیتابیس نمیخواند",
  db_row_not_retiring: "سرور در وضعیت بازنشستگی نیست",
  delete_protection_on: "حفاظت حذف روی سرور روشن است",
  // fleet planner
  settings_invalid: "تنظیمات ناوگان معتبر نیست",
  projection_incomplete: "پیش‌بینی هزینه کامل نیست",
  no_location_configured: "هیچ location تعیین نشده",
  spare_in_use: "جای خالی در حال استفاده است",
  no_other_location: "location دیگری در دسترس نیست",
  account_observation_stale: "داده‌ی حساب کهنه است",
  no_hetzner_id: "شناسه‌ی هتزنر ثبت نشده",
  server_not_observed: "سرور در هتزنر دیده نشد",
  no_active_account: "هیچ حساب هتزنر فعالی نیست",
  no_live_price: "قیمت زنده‌ی هتزنر هنوز خوانده نشده",
  // accounts
  resource_limit_exceeded: "هتزنر سقف منابع حساب را اعلام کرد",
  account_not_empty: "project سرور بیگانه دارد",
  verified: "توکن وارسی شد",
  foreign_servers: "سرور بیگانه در project",
  // owner actions
  ...SETTINGS_LABEL,
  owner_request: "به درخواست مالک",
};

/** Reasons stored as {"key": value} come as "key:value" (auth audit, counts). */
const REASON_KEY: Record<string, string> = {
  valid_minutes: "اعتبار (دقیقه)",
  count: "تعداد",
  remaining: "باقی‌مانده",
  method: "روش",
};

const REASON_VALUE: Record<string, string> = {
  totp: "کد Authenticator",
  recovery: "کد بازیابی",
};

export function reasonLabel(code: string): string {
  const known = REASON[code];
  if (known) return known;
  const i = code.indexOf(":");
  if (i > 0) {
    const key = code.slice(0, i);
    const value = code.slice(i + 1);
    const keyLabel = REASON_KEY[key] ?? REASON[key];
    if (keyLabel) return `${keyLabel}: ${REASON_VALUE[value] ?? toFa(value)}`;
  }
  return code;
}

const ACTION: Record<string, string> = {
  settings_update: "تغییر تنظیمات ناوگان",
  kill_switch_on: "روشن کردن کلید توقف",
  kill_switch_off: "خاموش کردن کلید توقف",
  account_add: "افزودن حساب هتزنر",
  account_active: "فعال شدن حساب",
  account_preparing: "آماده‌سازی حساب",
  account_draining: "تخلیه‌ی حساب",
  account_retired: "بازنشستگی حساب",
  account_locked: "قفل شدن حساب",
  account_guard: "نگهبان حساب",
  pricing_change: "تغییر قیمت هتزنر",
  learned_server_limit: "سقف سرور یادگرفته‌شده",
  setup_link_created: "ساخت لینک راه‌اندازی",
  setup_token_invalid: "لینک راه‌اندازی نامعتبر",
  setup_completed: "راه‌اندازی پنل و ساخت حساب مالک",
  login_ok: "ورود موفق",
  login_failed: "ورود ناموفق",
  logout: "خروج",
  session_revoked: "بستن جلسه",
  recovery_used: "ورود با کد بازیابی",
  recovery_codes_regenerated: "ساخت کدهای بازیابی تازه",
  recovery_regenerate_failed: "ساخت کدهای بازیابی رد شد",
  node_create: "ساخت node",
  node_delete: "حذف node",
  create: "ساخت سرور",
  delete: "حذف سرور",
  none: "بی کار",
};

export function actionLabel(code: string): string {
  return ACTION[code] ?? code;
}

export const PLAN_ACTION: Record<PlanActionKind, { label: string; tone: Tone }> = {
  create: { label: "ساخت", tone: "brand" },
  delete: { label: "حذف", tone: "danger" },
  none: { label: "بی کار", tone: "neutral" },
};

export function planActionInfo(kind: string): { label: string; tone: Tone } {
  return (PLAN_ACTION as Record<string, { label: string; tone: Tone } | undefined>)[kind] ?? {
    label: kind,
    tone: "neutral",
  };
}

const ACTOR: Record<string, string> = {
  fleet: "چرخه‌ی ناوگان",
  pricing: "چرخه‌ی قیمت",
  account_guard: "نگهبان حساب",
  auth: "سیستم ورود",
  worker: "worker",
  system: "سیستم",
};

/** "admin:owner" -> "مالک (owner)"; control loops get a Persian name. */
export function actorLabel(actor: string): string {
  if (actor.startsWith("admin:")) return `مالک (${actor.slice(6)})`;
  return ACTOR[actor] ?? actor;
}

export function isOwnerActor(actor: string): boolean {
  return actor.startsWith("admin:");
}
