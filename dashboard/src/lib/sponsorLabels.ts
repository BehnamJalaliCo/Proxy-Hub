// Persian names of the sponsor module's actions_log entries (panel/proxyhub/sponsor/service.py), for
// the events page. A file of its own with no imports, so lib/labels.ts can use it without a cycle.

/** The sponsor actions in the events page (proxyhub/sponsor/service.py). */
export const SPONSOR_ACTION: Record<string, string> = {
  "sponsor.proxy_found": "پروکسی تازه برای ثبت در ‎@MTProxybot‎",
  "sponsor.candidate_moved": "نشانی پروکسیِ هنوز ثبت‌نشده عوض شد",
  "sponsor.candidate_dropped": "کنار گذاشتن پروکسیِ ثبت‌نشده",
  "sponsor.bot_sent": "پیام به ‎@MTProxybot‎",
  "sponsor.bot_pressed": "زدن دکمه در ‎@MTProxybot‎",
  "sponsor.bot_reply": "پاسخ ‎@MTProxybot‎",
  "sponsor.registered": "ثبت پروکسی در ‎@MTProxybot‎",
  "sponsor.tag_applied": "گذاشتن tag اسپانسر روی node",
  "sponsor.tag_apply_failed": "گذاشتن tag روی node انجام نشد",
  "sponsor.promotion_set": "تنظیم کانال اسپانسر",
  "sponsor.flow_failed": "گفتگو با ‎@MTProxybot‎ به بعد افتاد",
  "sponsor.flow_stuck": "گفتگو با ‎@MTProxybot‎ متوقف شد",
  "sponsor.registration_retired": "بازنشستگی ثبت پروکسی",
  "sponsor.registration_reused": "استفاده‌ی دوباره از ثبت قبلی",
  "sponsor.retry_requested": "ثبت دوباره",
  "sponsor.settings_changed": "تغییر تنظیمات اسپانسر",
  "sponsor.campaign_created": "ساخت کمپین اسپانسر",
  "sponsor.campaign_updated": "ویرایش کمپین اسپانسر",
  "sponsor.campaign_cancelled": "لغو کمپین اسپانسر",
  "sponsor.campaign_deleted": "حذف کمپین شروع‌نشده",
  "sponsor.campaign_active": "شروع کمپین اسپانسر",
  "sponsor.campaign_finished": "پایان کمپین اسپانسر",
};
