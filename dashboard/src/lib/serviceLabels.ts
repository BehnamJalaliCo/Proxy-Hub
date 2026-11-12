// Persian words for the actions_log codes of the services around the fleet: channel posts, Reality,
// MTProto, candidate sites, ACME, analytics, cleanup and the sponsor loop (panel/proxyhub/*). A file
// of its own with no imports, so lib/labels.ts can use it without a cycle. labels.contract.test.ts
// reads the panel's sources and fails on any action or actor that has no word here or in labels.ts.

/** actions_log actions of these services. */
export const SERVICE_ACTION: Record<string, string> = {
  // channel posts (proxyhub/posts/publisher.py, api/routes_posts.py)
  "posts.queued": "پست تازه در صف انتشار",
  "posts.edit_queued": "ویرایش پست در صف (وضعیت کانفیگ‌ها)",
  "posts.edit_given_up": "ویرایش پست پس از چند بار تلاش رها شد",
  "posts.message_gone": "پیام پست در کانال دیگر نیست",
  "posts.settings_set": "تغییر تنظیمات انتشار",
  "posts.content_created": "ساخت محتوای تازه",
  "posts.content_updated": "ویرایش محتوا",
  "posts.content_status": "تغییر وضعیت محتوا",
  // Reality configs (proxyhub/reconcilers/reality.py)
  reality_config_created: "ساخت کانفیگ Reality تازه",
  reality_config_renewed: "تمدید کانفیگ Reality با همان لینک",
  reality_config_ended: "پایان کانفیگ Reality",
  reality_key_created: "ساخت کلید Reality برای جایگاه",
  reality_key_forgotten: "کنار گذاشتن کلید Reality یک node حذف‌شده",
  reality_target_switched: "عوض شدن سایت پوششی Reality",
  reality_target_risky: "سایت پوششی Reality پرخطر است",
  reality_target_failing: "سایت پوششی Reality جواب نمیدهد",
  // MTProto (proxyhub/mtproto/policy.py, sponsor_api.py)
  mtproto_config: "تنظیم MTProto روی node",
  mtproto_user_created: "ساخت کاربر MTProto",
  mtproto_extra_users_created: "ساخت کاربرهای اضافه‌ی MTProto برای پست",
  mtproto_extra_tags_synced: "هم‌سان کردن tag اسپانسر کاربرهای اضافه",
  mtproto_proxy_live: "پروکسی MTProto روی node بالا آمد",
  mtproto_users_disabled: "خاموش کردن کاربرهای MTProto یک node",
  mtproto_ad_tag: "تنظیم tag اسپانسر MTProto",
  // candidate sites (proxyhub/targets/service.py)
  target_auto_switched: "عوض شدن خودکار سایت پوششی",
  target_no_candidate: "هیچ سایت پوششی بازی نمانده",
  // the wildcard certificate (proxyhub/acme)
  acme_certificate_issued: "گرفتن گواهی TLS دامنه",
  acme_certificate_forgotten: "کنار گذاشتن گواهی دامنه‌ای که دیگر استفاده نمیشود",
  acme_order_failed: "گرفتن گواهی TLS ناموفق",
  // channel and server analytics (proxyhub/analytics)
  "analytics.settings_set": "تغییر تنظیمات آمار",
  "analytics.tracking_link_created": "ساخت لینک دعوت ردیابی",
  "analytics.tracking_link_renamed": "تغییر نام لینک دعوت ردیابی",
  "analytics.traffic_warning": "هشدار ترافیک سرور",
  "analytics.cpu_warning": "هشدار CPU سرور",
  // cleanup (proxyhub/cleanup)
  "cleanup.ended_purged": "پاک کردن پایان‌یافته‌ها از پنل",
  "telegram.test_channel_cleaned": "پاک کردن کانال آزمایشی",
  "telegram.test_channel_clean_refused": "پاک کردن کانال آزمایشی رد شد",
  // the sponsor loop's own checks (proxyhub/sponsor/inventory.py, D63)
  "sponsor.promotion_repair": "تنظیم دوباره‌ی کانال اسپانسری که ‎@MTProxybot‎ نشان نمیداد",
  "sponsor.promotion_alert": "هشدار: ‎@MTProxybot‎ هنوز کانال اسپانسر درست را نشان نمیدهد",
  // the login (proxyhub/auth): too many failures lock it for a while
  locked: "قفل موقت ورود پس از چند تلاش ناموفق",
  // a new Authenticator after a lost phone (proxyhub/auth)
  totp_reenrolled: "ثبت Authenticator تازه",
  totp_reenroll_failed: "ثبت Authenticator تازه رد شد",
};

/** settings_update of each service, by the event's target. */
export const SETTINGS_UPDATE_BY_TARGET: Record<string, string> = {
  fleet: "تغییر تنظیمات ناوگان",
  mtproto: "تغییر تنظیمات MTProto",
  reality: "تغییر تنظیمات Reality",
  targets: "تغییر تنظیمات سایت‌های پوششی",
};

/** The control loops of these services. */
export const SERVICE_ACTOR: Record<string, string> = {
  publisher: "چرخه‌ی انتشار پست",
  reality: "چرخه‌ی Reality",
  mtproto: "چرخه‌ی MTProto",
  "mtproto:sponsor_api": "API اسپانسر MTProto",
  targets: "چرخه‌ی سایت‌های پوششی",
  content_qc: "ناظر محتوا",
  sponsor: "چرخه‌ی اسپانسر",
  "sponsor:auto": "چرخه‌ی اسپانسر (وارسی خودکار)",
  acme: "چرخه‌ی گواهی TLS",
  analytics: "چرخه‌ی آمار",
};

/** Their reasons. */
export const SERVICE_REASON: Record<string, string> = {
  // channel posts
  slot_due: "نوبت پست برنامه‌ی روز",
  breaking: "خبر فوری",
  link_status: "وضعیت یک کانفیگ عوض شد",
  link_paused: "کانفیگی موقتاً از کار افتاد",
  link_moved: "نشانی یک کانفیگ عوض شد",
  message_gone: "پیام در کانال پیدا نشد (حذف شده)",
  edit_failed_repeatedly: "ویرایش چند بار پشت سر هم ناموفق بود",
  caption_limit: "کپشن عکس جای همه‌ی کانفیگ‌ها را نداشت",
  links_do_not_fit_caption: "کانفیگ‌ها در کپشن عکس جا نشدند",
  media_failed: "فایل عکس یا ویدیو گرفته نشد",
  media_waiting: "فایل عکس یا ویدیو هنوز آماده نبود",
  main_enabled: "انتشار در کانال اصلی روشن شد",
  main_disabled: "انتشار در کانال اصلی خاموش است",
  poster_operator: "پست‌ها با حساب دوم میروند",
  ambiguous_send: "پاسخ تلگرام نرسید",
  // why a post went with the bot (posts.poster_fallback)
  not_connected: "حساب دوم وصل نیست",
  no_username: "کانال نام کاربری عمومی ندارد",
  no_post_right: "حساب دوم اجازه‌ی ارسال پست ندارد",
  hold: "تلگرام از حساب دوم خواست صبر کند",
  request_refused: "پنل درخواست حساب دوم را مجاز ندانست",
  kill_switch: "کلید توقف روشن است",
  // Reality configs
  fresh_config: "کانفیگ تازه لازم بود",
  still_working: "کانفیگ هنوز کار میکند",
  new_scope: "جایگاه یا node تازه",
  node_deleted: "node حذف شد",
  full: "حجمش پر شد",
  expired: "زمانش تمام شد",
  target_replaced: "سایت پوششی عوض شد",
  risky_sni: "سایت پوششی پرخطر است",
  old_target_failed: "سایت پوششی قبلی جواب نمیداد",
  no_active_configs: "کانفیگ فعالی روی سایت قبلی نبود",
  target_check_failed: "وارسی سایت پوششی ناموفق بود",
  // candidate sites
  auto: "انتخاب خودکار",
  not_a_candidate: "سایت فعلی در فهرست سایت‌های پوششی نیست",
  node_check_failed: "وارسی سایت از روی node ناموفق بود",
  blocked_from_iran: "سایت فعلی از ایران بسته است",
  no_open_candidate: "هیچ سایت پوششی باز از ایران نمانده",
  // MTProto
  created: "تازه ساخته شد",
  settings_changed: "تنظیمات عوض شد",
  one_proxy_per_node: "هر node یک پروکسی دارد",
  user_missing: "کاربر روی node نبود",
  links_per_post: "لینک‌های لازم برای هر پست",
  tag_set: "tag اسپانسر گذاشته شد",
  tag_cleared: "tag اسپانسر برداشته شد",
  agent_applied: "عامل node تنظیم را اجرا کرد",
  telemt_up: "telemt بالا آمد",
  node_burned: "node سوخت",
  node_retiring: "node در حال بازنشستگی است",
  node_draining: "node در حال تخلیه است",
  // ACME
  "dns-01": "وارسی DNS-01",
  zone_gone: "دامنه دیگر در Cloudflare نیست",
  pool_none: "دامنه از استخر بیرون رفت",
  pool_filtered: "دامنه فیلتر شد",
  pool_retired: "دامنه بازنشسته شد",
  pool_deleted: "دامنه از Cloudflare پاک شد",
  acme_validation_failed: "Let's Encrypt وارسی DNS را نپذیرفت",
  acme_timeout: "Let's Encrypt در زمان مقرر پاسخ نداد",
  acme_unreachable: "Let's Encrypt در دسترس نبود",
  acme_client_error: "Let's Encrypt درخواست را نپذیرفت",
  acme_unexpected: "پاسخ نامنتظره از Let's Encrypt",
  acme_rateLimited: "سقف درخواست Let's Encrypt پر شده",
  acme_no_dns01: "Let's Encrypt راه DNS-01 نداد",
  acme_no_certificate: "Let's Encrypt گواهی را نفرستاد",
  acme_two_values_one_name: "دو مقدار وارسی برای یک نام",
  dns_conflict: "رکورد DNS دیگری سر راه است",
  dns_publish_failed: "رکورد وارسی در Cloudflare ثبت نشد",
  dns_not_propagated: "رکورد وارسی هنوز پخش نشده بود",
  // analytics
  traffic_over_threshold: "ترافیک از آستانه گذشت",
  cpu_over_threshold: "CPU از آستانه گذشت",
  // cleanup
  D44: "دکمه‌ی مالک (D44)",
  budget: "سهم این دور تمام شد",
  bot_not_admin: "ربات در کانال ادمین نیست",
  bot_cannot_delete: "ربات اجازه‌ی حذف پیام ندارد",
  bot_token_invalid: "توکن ربات پذیرفته نشد",
  channel_not_found: "کانال پیدا نشد",
  // the content supervisor
  qc_rejects: "ردهای پشت سر هم ناظر",
  approve: "تأیید",
  reject: "رد",
  // the sponsor loop
  tag_received: "tag اسپانسر از ‎@MTProxybot‎ رسید",
  proxy_gone: "پروکسی دیگر نیست",
  registration_exists: "پیش‌تر ثبت شده",
  address_changed: "نشانی پروکسی عوض شد",
  no_registration: "هنوز ثبت نشده",
  same_address_and_user: "همان نشانی و همان کاربر",
  node_gone: "node دیگر نیست",
  set_promotion: "تنظیم کانال اسپانسر",
  never_started: "هنوز شروع نشده بود",
  removed_in_bot: "در ‎@MTProxybot‎ پاک شده بود",
  apply_ad_tag: "گذاشتن tag روی node",
  complete: "کامل خوانده شد",
  incomplete: "کامل خوانده نشد",
  inventory: "خواندن فهرست",
  cleanup: "پاک کردن پروکسی‌های مرده",
  look: "فقط دیدن",
  observe: "دیدن صفحه‌ی هر پروکسی",
  remove: "حذف واقعی",
  done: "تمام شد",
  stopped: "متوقف شد",
  failed: "ناموفق",
  stuck: "گیر کرد",
  on: "روشن",
  off: "خاموش",
  ok: "درست است",
  other: "کانال دیگری را نشان میدهد",
  unread: "هنوز خوانده نشده",
  not_shown: "هیچ کانالی نشان داده نمیشود",
  several: "چند کانال نشان داده میشود",
  remove_button_found: "دکمه‌ی حذف پیدا شد",
  // which factor of a refused login or Authenticator change was wrong (proxyhub/auth)
  "factor:password": "رمز درست نبود",
  "factor:second_factor": "کد Authenticator یا کد بازیابی درست نبود",
  "factor:new_code": "کد Authenticator تازه درست نبود",
  "factor:pending": "مهلت یک‌ساعته‌ی ثبت Authenticator تازه گذشته بود",
  "factor:unknown_user": "نام کاربری ناشناس",
  "scope:user": "قفل برای این نام کاربری",
  "scope:global": "قفل برای همه‌ی ورودها",
};

/** Setting keys of the Reality, MTProto and candidate-site forms (settings_update reasons). */
export const SETTINGS_KEY: Record<string, string> = {
  enabled: "روشن یا خاموش",
  transport: "روش انتقال",
  xhttp_enabled: "XHTTP",
  configs_per_node: "کانفیگ در هر node",
  cap_hours: "عمر کانفیگ (ساعت)",
  cap_gb: "سقف حجم کانفیگ (گیگابایت)",
  renew: "تمدید خودکار",
  target_mode: "انتخاب سایت پوششی",
  target_host: "سایت پوششی",
  target_port: "پورت سایت پوششی",
  link_name: "نام لینک",
  port: "پورت",
  tls_domain: "دامنه‌ی TLS",
  mode_tls: "حالت TLS",
  mode_secure: "حالت secure",
  mode_classic: "حالت classic",
  candidates: "فهرست سایت‌های پوششی",
  interval_h: "فاصله‌ی وارسی (ساعت)",
};

/** "key:value" reasons of these services. */
export const SERVICE_REASON_KEY: Record<string, string> = {
  from: "از وضعیت",
  level: "مرحله‌ی قفل",
};

/** States named inside a reason ("from:registered", "scheduled->active"). */
export const SERVICE_STATE: Record<string, string> = {
  pending: "در انتظار ثبت",
  registered: "ثبت‌شده",
  failed: "متوقف",
  retired: "بازنشسته",
  draft: "پیش‌نویس",
  scheduled: "زمان‌بندی‌شده",
  active: "در حال اجرا",
  finished: "تمام‌شده",
  cancelled: "لغو شده",
};
