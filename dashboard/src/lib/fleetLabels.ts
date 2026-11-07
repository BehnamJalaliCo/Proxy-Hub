// Persian words for the live fleet's machine codes (panel/proxyhub/fleet/*, nodes/executor.py,
// cloudflare/delegation.py): actions_log actions, reasons and node state-change reasons. A file of
// its own with no imports, so lib/labels.ts can use it without a cycle.

/** actions_log actions of the live fleet and the delegation check. */
export const FLEET_ACTION: Record<string, string> = {
  node_request: "درخواست سرور برای جایگاه",
  node_retire: "بازنشستگی node جایگاه",
  node_burned: "سوختن node (بسته از ایران)",
  node_burned_ip: "IP سوخته؛ سرور تازه همان دم بازنشسته شد",
  fleet_alert: "هشدار ناوگان به مالک",
  request_backoff: "صبر پس از رد شدن درخواست",
  slot_unhealthy: "جایگاه ناسالم (مهلت ترمیم)",
  domain_delegation: "وارسی ثبت و nameserverهای دامنه",
};

/** Reasons: the fleet planner, the executor's refusals, retirements and the delegation verdicts. */
export const FLEET_REASON: Record<string, string> = {
  // planner (proxyhub/fleet/planner.py)
  slot_healthy: "جایگاه سالم است",
  slot_starting: "سرور این جایگاه در راه است",
  slot_unhealthy: "جایگاه ناسالم است؛ مهلت ترمیم (۱۵ دقیقه) در جریان است",
  replacement_settling: "سرور جایگزین تازه آماده شد؛ چند دقیقه صبر تا جابه‌جایی DNS",
  making_room: "حساب هتزنر پر است؛ سرور سوخته اول حذف میشود تا جا برای جایگزین باز شود",
  replacement_in_progress: "سرور جایگزین در راه است",
  request_backoff: "درخواست قبلی این جایگاه رد شد؛ ۱۵ دقیقه صبر",
  orphan_server_not_in_db: "سروری با برچسب ما که در پنل ثبت نیست (هرگز حذف نمیشود)",
  // why a node leaves (node_operations.retire_reason)
  burned: "از ایران بسته شد (سوخته)",
  unhealthy: "ناسالم ماند و جایگزین شد",
  duplicate: "سرور تازه‌تر جای آن را گرفت",
  excess: "بیرون از جایگاه‌های تعیین‌شده",
  burned_ip: "IP سوخته یا IP از رنج /24 سوخته گرفت",
  burned_prefix24: "IP در رنج /24 سوخته بود",
  verdict_blocked: "هر دو منبع سنجش: از ایران بسته است",
  // the executor and the nodes API
  fleet_dry_run: "ناوگان در حالت خشک است؛ سرور جایگاه‌ها نه ساخته میشود نه حذف",
  fleet_live_disabled: "حالت زنده‌ی ناوگان در کد بسته است؛ سرور جایگاه‌ها نه ساخته میشود نه حذف",
  fleet_managed: "node ناوگان را خود ناوگان جایگزین یا بازنشسته میکند",
  // owner alerts (fleet_alerts.code)
  delete_blocked: "قفل حذف، حذف یک سرور را رد میکند",
  burned_ip_streak: "چند سرور پشت سر هم IP سوخته گرفتند",
  // lost registration or delegation (proxyhub/cloudflare/delegation.py)
  delegation_lost: "ثبت یا nameserverهای دامنه دیگر روی Cloudflare نیست",
  delegation_restored: "دامنه دوباره روی Cloudflare است",
  replaces_delegation_lost: "جای دامنه‌ای را گرفت که از Cloudflare بیرون رفت",
  delegated_to_cloudflare: "nameserverها روی Cloudflare است",
  lost: "از دست رفته",
  nxdomain: "دامنه پیدا نمیشود (NXDOMAIN)",
  not_cloudflare: "nameserverها روی Cloudflare نیست",
  ns_changed: "nameserverها به Cloudflare دیگری رفته",
  no_answer: "پاسخ بی nameserver",
  servfail: "خطای resolver (SERVFAIL)",
  no_conclusive_lookup: "هنوز وارسی قطعی‌ای نیست",
  single_lookup: "فقط یک وارسی؛ دو وارسی لازم است",
  lookups_too_close: "دو وارسی آخر کمتر از ۳۰ دقیقه فاصله دارند",
  lookups_too_old: "وارسی‌ها کهنه‌تر از ۶ ساعت‌اند",
};

/** node_events detail.reason of the fleet's state changes (the loop prefixes them with "fleet_"). */
export const FLEET_TRANSITION: Record<string, string> = {
  verdict_blocked: "هر دو منبع سنجش: از ایران بسته است",
  fleet_burned: "ناوگان: سوخته بود و جایگزینش آماده شد",
  fleet_unhealthy: "ناوگان: ناسالم ماند و جایگزین شد",
  fleet_duplicate: "ناوگان: سرور تازه‌تر جایش را گرفت",
  fleet_excess: "ناوگان: بیرون از جایگاه‌های تعیین‌شده",
  burned_ip: "IP سوخته گرفت",
};

/** node_events kinds the fleet adds. */
export const FLEET_NODE_EVENT: Record<string, string> = {
  requested: "درخواست ساخت",
  burned_ip: "IP سوخته؛ بازنشسته شد",
};
