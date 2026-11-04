import { ChartColumnBig, Cloud, LayoutDashboard, Megaphone, Network, Newspaper, ScrollText, Send, Server, ServerCog, Settings, ShieldCheck, Zap, type LucideIcon } from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  short: string;
  description: string;
  icon: LucideIcon;
  /** In the phone's bottom bar (four items + "more"); every item is in the side menu. */
  bottom?: boolean;
}

export const NAV: NavItem[] = [
  { to: "/", label: "نمای کلی", short: "نمای کلی", description: "هزینه، ناوگان، بودجه و وضعیت", icon: LayoutDashboard, bottom: true },
  { to: "/stats", label: "آمار", short: "آمار", description: "ترافیک، هزینه، کاربران و رشد کانال", icon: ChartColumnBig },
  { to: "/nodes", label: "nodeها", short: "nodeها", description: "سرورها، عامل‌ها و باز بودن از ایران", icon: ServerCog, bottom: true },
  { to: "/telegram", label: "تلگرام", short: "تلگرام", description: "ربات، کانال‌ها و زمان‌بندی پست", icon: Send, bottom: true },
  { to: "/mtproto", label: "پروکسی‌های MTProto", short: "MTProto", description: "لینک‌ها، اتصال‌ها و تنظیم Telemt", icon: Network },
  { to: "/sponsor", label: "اسپانسر", short: "اسپانسر", description: "ثبت در ‎@MTProxybot‎ و کمپین‌های اسپانسر", icon: Megaphone },
  { to: "/content", label: "محتوا و انتشار", short: "محتوا", description: "پست خودکار، پیش‌نمایش و کتابخانه‌ی محتوا", icon: Newspaper },
  { to: "/reality", label: "Reality", short: "Reality", description: "کانفیگ‌های VLESS، زمان و حجم مانده", icon: Zap },
  { to: "/accounts", label: "حساب‌های هتزنر", short: "حساب‌ها", description: "افزودن، تخلیه و بازنشستگی", icon: Server },
  { to: "/cloudflare", label: "Cloudflare و دامنه‌ها", short: "دامنه‌ها", description: "توکن، استخر دامنه و رکوردها", icon: Cloud },
  { to: "/events", label: "رویدادها", short: "رویدادها", description: "هر کاری که سیستم کرده، با دلیل", icon: ScrollText },
  { to: "/settings", label: "تنظیمات ناوگان", short: "تنظیمات", description: "سقف‌ها، هدف‌ها و کلید توقف", icon: Settings, bottom: true },
  { to: "/security", label: "امنیت", short: "امنیت", description: "جلسه‌ها و کدهای بازیابی", icon: ShieldCheck },
];

export const BOTTOM_NAV = NAV.filter((n) => n.bottom === true);

export function titleFor(pathname: string): string {
  const item = NAV.find((n) => (n.to === "/" ? pathname === "/" : pathname === n.to || pathname.startsWith(`${n.to}/`)));
  return item?.label ?? "پنل";
}
