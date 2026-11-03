// Persian labels for machine codes the API returns (states, reasons, actions). Unknown codes are
// shown as they are, so a new code on the server is never hidden.

import type { AccountState, FleetSettings, NodeState, PlanActionKind, PoolState, ProbeVerdict } from "./types";

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
  prices_stale: "قیمت‌های هتزنر کهنه است (بیش از ۶ ساعت)",
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
  // nodes (team live): refusals of a test-node request or a retirement
  agent_domain_not_set: "دامنه‌ی سایت عامل‌ها (AGENT_DOMAIN) تنظیم نشده",
  location_not_allowed: "این location در فهرست مجاز تنظیمات نیست",
  no_usable_account: "هیچ حساب هتزنر آماده‌ای برای ساخت نیست",
  test_node_exists: "یک node آزمایشی زنده هست",
  token_missing: "توکن این حساب هتزنر در گاوصندوق نیست",
  user_data_failed: "ساختن cloud-init این node ناموفق بود",
  vault_unavailable: "کلید رمزنگاری پنل در دسترس نیست",
  node_changed_meanwhile: "وضعیت node هم‌زمان عوض شد؛ دوباره امتحان کنید",
  node_deleted: "این node پیش‌تر حذف شده",
  fleet_not_live: "ناوگان ۹ جایگاهی فقط حالت خشک دارد؛ فقط node آزمایشی حذف‌شدنی است",
  hetzner_unreachable: "API هتزنر پاسخ نداد",
  hetzner_rate_limited: "سقف درخواست API هتزنر پر شده",
  hetzner_token_invalid: "هتزنر توکن این حساب را نپذیرفت",
  hetzner_forbidden: "هتزنر دسترسی این توکن را رد کرد",
  hetzner_read_only: "توکن این حساب فقط‌خواندنی است",
  hetzner_api_error: "API هتزنر خطا داد",
  unhealthy_test_node: "node آزمایشی یک ساعت ناسالم ماند",
  owner_retire: "بازنشستگی به درخواست مالک",
  // Telegram (team tg)
  getme_ok: "ربات با getMe وارسی شد",
  bot_can_delete: "ربات اجازه‌ی حذف پیام دارد؛ باید برداشته شود",
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
  // nodes (team live) and the node agents
  test_node_request: "درخواست node آزمایشی",
  node_retire_request: "درخواست بازنشستگی node",
  node_create_failed: "ساخت node ناموفق",
  node_ready: "آماده شدن node",
  agent_register: "ثبت عامل node",
  agent_renew: "تمدید گواهی عامل",
  // Telegram (team tg)
  "telegram.bot_set": "ثبت ربات تلگرام",
  "telegram.pairing_created": "ساخت کد اتصال تلگرام",
  "telegram.pairing_cancelled": "باطل شدن کد اتصال تلگرام",
  "telegram.owner_paired": "وصل شدن چت مالک به ربات",
  "telegram.channel_set": "ثبت کانال تلگرام",
  "telegram.settings_set": "تغییر زمان‌بندی پست",
  "telegram.test_alert": "هشدار آزمایشی",
  "telegram.test_post": "پست آزمایشی",
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
  node_executor: "چرخه‌ی nodeها",
  agent_api: "API عامل‌ها",
  telegram: "ربات تلگرام",
};

/** "admin:owner" -> "مالک (owner)"; control loops get a Persian name. */
export function actorLabel(actor: string): string {
  if (actor.startsWith("admin:")) return `مالک (${actor.slice(6)})`;
  return ACTOR[actor] ?? actor;
}

export function isOwnerActor(actor: string): boolean {
  return actor.startsWith("admin:");
}

// --- round 2: error codes ---------------------------------------------------------------------

/** Persian text of the round-2 error codes, used when a response carries no message_fa. */
const ERROR_FA: Record<string, string> = {
  // nodes
  gate_refused: "قفل‌های ایمنی این کار را رد کردند. دلیل‌ها در فهرست آمده‌اند.",
  test_node_exists: "یک node آزمایشی زنده هست. اول همان را بازنشسته کنید.",
  not_found: "پیدا نشد؛ شاید پیش‌تر حذف شده است.",
  // Telegram
  bot_token_invalid: "توکن ربات درست نیست یا در BotFather باطل شده است.",
  bot_not_set: "هنوز توکن ربات تلگرام وارد نشده است.",
  owner_not_paired: "چت خصوصی شما هنوز به ربات وصل نشده است. اول کد اتصال بسازید.",
  channel_not_found: "کانال پیدا نشد. نام کانال را درست وارد کنید (مثل ‎@ipoxxo‎).",
  bot_not_admin: "ربات در این کانال ادمین نیست.",
  bot_cannot_post: "ربات در این کانال اجازه‌ی ارسال پیام ندارد.",
  bot_cannot_edit: "ربات در این کانال اجازه‌ی ویرایش پیام ندارد.",
  channel_in_use: "این کانال برای نقش دیگر (اصلی یا آزمایشی) ثبت شده است.",
  test_channel_not_set: "کانال آزمایشی هنوز تعیین نشده است.",
  invalid_settings: "زمان‌بندی درست نیست؛ ساعت‌ها و فاصله‌ی پست‌ها را بررسی کنید.",
  telegram_unavailable: "پاسخ تلگرام به دست نیامد. چند دقیقه‌ی دیگر دوباره امتحان کنید.",
  vault_unavailable: "کلید رمزنگاری پنل در دسترس نیست؛ کار انجام نشد.",
  // Cloudflare
  cf_token_invalid: "Cloudflare این توکن را نپذیرفت.",
  cf_permissions_missing: "توکن Cloudflare همه‌ی دسترسی‌های لازم را ندارد.",
  cf_unavailable: "پاسخ Cloudflare به دست نیامد. کمی بعد دوباره امتحان کنید.",
};

export function errorCodeText(code: string): string | undefined {
  return ERROR_FA[code];
}

// --- round 2: nodes -----------------------------------------------------------------------------

export interface StateInfo {
  label: string;
  tone: Tone;
  hint: string;
  /** Moving on by itself (the badge pulses). */
  moving: boolean;
}

export const NODE_STATE: Record<NodeState, StateInfo> = {
  requested: { label: "درخواست‌شده", tone: "info", hint: "در صف ساخت؛ چرخه‌ی nodeها دوباره همه‌ی قفل‌های ایمنی را میسنجد.", moving: true },
  creating: { label: "در حال ساخت", tone: "info", hint: "سرور در هتزنر ساخته میشود.", moving: true },
  booting: { label: "در حال روشن شدن", tone: "info", hint: "سرور روشن شد؛ cloud-init در حال آماده‌سازی است.", moving: true },
  registering: { label: "ثبت عامل", tone: "info", hint: "عامل node در پنل ثبت شد و اولین گزارش را میفرستد.", moving: true },
  ready: { label: "آماده", tone: "success", hint: "node سالم است و پروکسی آن کار میکند.", moving: false },
  unhealthy: { label: "ناسالم", tone: "danger", hint: "عامل گزارش سالم نفرستاده است؛ پنل خودش ترمیم یا جایگزین میکند.", moving: false },
  repairing: { label: "در حال ترمیم", tone: "warning", hint: "پنل در حال ترمیم خودکار این node است.", moving: true },
  burned: { label: "سوخته (فیلتر)", tone: "danger", hint: "IP این node از ایران بسته است و جایگزین میشود.", moving: false },
  draining: { label: "در حال تخلیه", tone: "warning", hint: "کانفیگ تازه‌ای به این node نمیرود.", moving: true },
  retiring: { label: "در حال بازنشستگی", tone: "warning", hint: "سرور با قفل حذف (برچسب و ثبت در پنل) حذف میشود.", moving: true },
  deleted: { label: "حذف‌شده", tone: "neutral", hint: "سرور در هتزنر حذف شده و دیگر هزینه ندارد.", moving: false },
};

export function nodeStateInfo(state: string): StateInfo {
  return (NODE_STATE as Record<string, StateInfo | undefined>)[state] ?? { label: state, tone: "neutral", hint: "", moving: false };
}

/** Hetzner locations by code; anything else is shown as its code. */
const LOCATION_NAME: Record<string, string> = {
  fsn1: "فالکنشتاین، آلمان",
  nbg1: "نورنبرگ، آلمان",
  hel1: "هلسینکی، فنلاند",
  ash: "اَشبرن، آمریکا",
  hil: "هیلزبورو، آمریکا",
  sin: "سنگاپور",
};

export function locationName(code: string): string {
  return LOCATION_NAME[code] ?? code;
}

const NODE_EVENT: Record<string, string> = {
  state: "تغییر وضعیت",
  create_started: "شروع ساخت سرور",
  create_refused: "ساخت رد شد",
  create_failed: "ساخت ناموفق",
  server_created: "سرور ساخته شد",
  server_adopted: "سرور با برچسب پیدا شد",
  no_server_found: "سروری با برچسب پیدا نشد",
  delete_started: "شروع حذف سرور",
  delete_refused: "حذف رد شد",
  server_deleted: "سرور حذف شد",
  server_already_gone: "سرور پیش‌تر حذف شده بود",
  executor_error: "خطای چرخه‌ی nodeها",
  agent_registered: "عامل ثبت شد",
  agent_cert_renewed: "گواهی عامل تمدید شد",
};

export function nodeEventLabel(kind: string): string {
  return NODE_EVENT[kind] ?? kind;
}

/** Why a node changed state (node_events detail.reason). */
const TRANSITION_REASON: Record<string, string> = {
  gates_passed: "همه‌ی قفل‌های ایمنی سبز بود",
  server_found_by_label: "سرور با برچسب پیدا شد",
  server_created: "هتزنر ساخت را تأیید کرد",
  agent_registered: "عامل ثبت شد",
  agent_reported: "اولین گزارش سالم رسید",
  agent_recovered: "عامل دوباره سالم شد",
  boot_timeout: "روشن شدن بیش از حد طول کشید",
  no_good_report: "گزارش سالمی نرسید",
  heartbeat_lost: "ضربان عامل قطع شد",
  report_not_good: "گزارش عامل سالم نبود",
  create_refused: "قفل‌های ایمنی ساخت را رد کردند",
  create_failed: "هتزنر ساخت را تأیید نکرد",
  owner_retire: "به درخواست مالک",
  server_deleted: "سرور در هتزنر حذف شد",
  unhealthy_test_node: "node آزمایشی یک ساعت ناسالم ماند",
};

export function transitionReasonLabel(code: string): string {
  return TRANSITION_REASON[code] ?? reasonLabel(code);
}

// --- round 2: Cloudflare and probes ---------------------------------------------------------------

export const POOL_STATE: Record<PoolState, { label: string; tone: Tone; hint: string }> = {
  active: { label: "فعال", tone: "success", hint: "نام‌های پروکسی پست‌های تازه روی این دامنه‌اند. فقط یک دامنه فعال است." },
  standby: { label: "آماده‌به‌کار", tone: "info", hint: "اگر دامنه‌ی فعال فیلتر شد، خودکار جایش را میگیرد." },
  none: { label: "بیرون از استخر", tone: "neutral", hint: "پنل از این دامنه استفاده نمیکند." },
  filtered: { label: "فیلترشده", tone: "danger", hint: "از ایران بسته است؛ برای پست تازه استفاده نمیشود." },
  retired: { label: "بازنشسته", tone: "neutral", hint: "کنار گذاشته شده و دیگر استفاده نمیشود." },
};

export const POOL_ORDER: PoolState[] = ["active", "standby", "none", "filtered", "retired"];

export function poolStateInfo(state: string): { label: string; tone: Tone; hint: string } {
  return (POOL_STATE as Record<string, { label: string; tone: Tone; hint: string } | undefined>)[state] ?? {
    label: state,
    tone: "neutral",
    hint: "",
  };
}

/** Cloudflare's own zone status (plus "gone": the token no longer lists the zone). */
const ZONE_STATUS: Record<string, { label: string; tone: Tone }> = {
  active: { label: "فعال در Cloudflare", tone: "success" },
  pending: { label: "در انتظار nameserver", tone: "warning" },
  initializing: { label: "در حال راه‌اندازی", tone: "info" },
  moved: { label: "nameserver عوض شده", tone: "danger" },
  deleted: { label: "حذف‌شده در Cloudflare", tone: "danger" },
  deactivated: { label: "غیرفعال در Cloudflare", tone: "danger" },
  gone: { label: "دیگر در دسترس توکن نیست", tone: "danger" },
};

export function zoneStatusInfo(status: string): { label: string; tone: Tone } {
  return ZONE_STATUS[status] ?? { label: status, tone: "neutral" };
}

export const VERDICT: Record<ProbeVerdict, { label: string; tone: Tone; hint: string }> = {
  ok: { label: "باز از ایران", tone: "success", hint: "از بیشتر شبکه‌های ایران وصل میشود." },
  suspect: { label: "مشکوک", tone: "warning", hint: "از بعضی شبکه‌ها وصل نمیشود؛ دوباره سنجیده میشود." },
  blocked: { label: "فیلتر", tone: "danger", hint: "از ایران بسته است." },
  unknown: { label: "نامعلوم", tone: "neutral", hint: "هنوز سنجش کافی نیست." },
};

export function verdictInfo(v: string): { label: string; tone: Tone; hint: string } {
  return (VERDICT as Record<string, { label: string; tone: Tone; hint: string } | undefined>)[v] ?? { label: v, tone: "neutral", hint: "" };
}

export const PROBE_SOURCE: Record<string, string> = {
  globalping: "Globalping",
  "check-host": "check-host.net",
};

/** Persian names of Iranian operators by ASN, in a fixed display order (mobile operators first).
 * Only well-known ASNs; any other network keeps its own name. */
const OPERATORS: [number, string][] = [
  [197207, "همراه اول"],
  [44244, "ایرانسل"],
  [57218, "رایتل"],
  [58224, "مخابرات ایران"],
];
const OPERATOR_BY_ASN = new Map(OPERATORS);

export function operatorName(asn: number | null): string | null {
  return asn === null ? null : (OPERATOR_BY_ASN.get(asn) ?? null);
}

/** Display rank of a well-known operator (lower first); null for any other network. */
export function operatorRank(asn: number | null): number | null {
  const i = OPERATORS.findIndex(([a]) => a === asn);
  return i < 0 ? null : i;
}

/** Documented tg addition: why the bot is not usable right now. */
const BOT_ERROR: Record<string, string> = {
  bot_unauthorized: "تلگرام توکن ربات را دیگر نمیپذیرد (در BotFather باطل یا عوض شده). توکن تازه وارد کنید.",
  updates_conflict: "برنامه‌ی دیگری پیام‌های این ربات را میخواند (webhook یا polling دیگر)؛ تا قطع نشود، اتصال چت کار نمیکند.",
};

export function botErrorText(code: string): string {
  return BOT_ERROR[code] ?? code;
}
