import { LayoutDashboard, ScrollText, Server, Settings, ShieldCheck, type LucideIcon } from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  short: string;
  description: string;
  icon: LucideIcon;
}

export const NAV: NavItem[] = [
  { to: "/", label: "نمای کلی", short: "نمای کلی", description: "هزینه، ناوگان، بودجه و وضعیت", icon: LayoutDashboard },
  { to: "/accounts", label: "حساب‌های هتزنر", short: "حساب‌ها", description: "افزودن، تخلیه و بازنشستگی", icon: Server },
  { to: "/events", label: "رویدادها", short: "رویدادها", description: "هر کاری که سیستم کرده، با دلیل", icon: ScrollText },
  { to: "/settings", label: "تنظیمات ناوگان", short: "تنظیمات", description: "سقف‌ها، هدف‌ها و کلید توقف", icon: Settings },
  { to: "/security", label: "امنیت", short: "امنیت", description: "جلسه‌ها و کدهای بازیابی", icon: ShieldCheck },
];

export function titleFor(pathname: string): string {
  const item = NAV.find((n) => (n.to === "/" ? pathname === "/" : pathname.startsWith(n.to)));
  return item?.label ?? "پنل";
}
