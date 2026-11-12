import { ChartColumnBig, Cloud, Inbox, LayoutDashboard, Megaphone, Network, Newspaper, Rss, ScrollText, Send, Server, ServerCog, Settings, ShieldCheck, Zap, type LucideIcon } from "lucide-react";

export type NavGroupId = "view" | "fleet" | "channel" | "system";

export interface NavItem {
  to: string;
  label: string;
  short: string;
  description: string;
  icon: LucideIcon;
  group: NavGroupId;
  /** In the phone's bottom bar (four items + "more"); every item is in the side menu. */
  bottom?: boolean;
}

/** The side menu's sections, in order. */
export const NAV_GROUPS: { id: NavGroupId; label: string }[] = [
  { id: "view", label: "نما" },
  { id: "fleet", label: "ناوگان" },
  { id: "channel", label: "کانال" },
  { id: "system", label: "سیستم" },
];

export const NAV: NavItem[] = [
  { to: "/", label: "نمای کلی", short: "نمای کلی", description: "هزینه، ناوگان، بودجه و وضعیت", icon: LayoutDashboard, group: "view", bottom: true },
  { to: "/stats", label: "آمار", short: "آمار", description: "ترافیک، هزینه، کاربران و رشد کانال", icon: ChartColumnBig, group: "view" },
  { to: "/nodes", label: "nodeها", short: "nodeها", description: "سرورها، عامل‌ها و باز بودن از ایران", icon: ServerCog, group: "fleet", bottom: true },
  { to: "/mtproto", label: "پروکسی‌های MTProto", short: "MTProto", description: "لینک‌ها، اتصال‌ها و تنظیم Telemt", icon: Network, group: "fleet" },
  { to: "/reality", label: "Reality", short: "Reality", description: "کانفیگ‌های VLESS، زمان و حجم مانده", icon: Zap, group: "fleet" },
  { to: "/accounts", label: "حساب‌های هتزنر", short: "حساب‌ها", description: "افزودن، تخلیه و بازنشستگی", icon: Server, group: "fleet" },
  { to: "/cloudflare", label: "Cloudflare و دامنه‌ها", short: "دامنه‌ها", description: "توکن، استخر دامنه و رکوردها", icon: Cloud, group: "fleet" },
  { to: "/telegram", label: "تلگرام", short: "تلگرام", description: "ربات، کانال‌ها و زمان‌بندی پست", icon: Send, group: "channel", bottom: true },
  { to: "/content", label: "محتوا و انتشار", short: "محتوا", description: "پست خودکار، پیش‌نمایش و کتابخانه‌ی محتوا", icon: Newspaper, group: "channel" },
  { to: "/review", label: "صف بازبینی", short: "بازبینی", description: "خبرها پیش از رفتن به کانال؛ تأیید، ویرایش یا رد", icon: Inbox, group: "channel" },
  { to: "/sources", label: "منبع‌های محتوا", short: "منبع‌ها", description: "کانال‌های عمومی تلگرام و خوراک‌های RSS", icon: Rss, group: "channel" },
  { to: "/sponsor", label: "اسپانسر", short: "اسپانسر", description: "ثبت در ‎@MTProxybot‎ و کمپین‌های اسپانسر", icon: Megaphone, group: "channel" },
  { to: "/events", label: "رویدادها", short: "رویدادها", description: "هر کاری که سیستم کرده، با دلیل", icon: ScrollText, group: "system" },
  { to: "/settings", label: "تنظیمات ناوگان", short: "تنظیمات", description: "سقف‌ها، هدف‌ها و کلید توقف", icon: Settings, group: "system", bottom: true },
  { to: "/security", label: "امنیت", short: "امنیت", description: "جلسه‌ها و کدهای بازیابی", icon: ShieldCheck, group: "system" },
];

export const BOTTOM_NAV = NAV.filter((n) => n.bottom === true);

export function titleFor(pathname: string): string {
  const item = NAV.find((n) => (n.to === "/" ? pathname === "/" : pathname === n.to || pathname.startsWith(`${n.to}/`)));
  return item?.label ?? "پنل";
}
