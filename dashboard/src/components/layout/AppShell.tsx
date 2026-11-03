import { useQueryClient } from "@tanstack/react-query";
import { FlaskConical, LogOut, Menu, OctagonPause, UserRound, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Dialog } from "radix-ui";
import { useEffect, useState } from "react";
import { Link, NavLink, useLocation, useNavigate, useOutlet } from "react-router";

import { cn } from "../../lib/browser";
import * as api from "../../lib/endpoints";
import { formatDurationSeconds } from "../../lib/format";
import { useMe, useOverview } from "../../lib/queries";
import { toast } from "../../lib/toast";
import { StatusDot } from "../ui/Card";
import { Logo, ThemeSwitch, ThemeToggleButton } from "./Brand";
import { NAV, titleFor } from "./nav";

function useLogout(): [boolean, () => void] {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const run = () => {
    setBusy(true);
    void api
      .logout()
      .catch(() => undefined)
      .finally(() => {
        qc.clear();
        setBusy(false);
        void navigate("/login", { replace: true });
        toast.info("از پنل خارج شدید");
      });
  };
  return [busy, run];
}

function isActive(pathname: string, to: string): boolean {
  return to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(`${to}/`);
}

function SideNav({ onNavigate, withDescriptions = false }: { onNavigate?: () => void; withDescriptions?: boolean }) {
  const { pathname } = useLocation();
  return (
    <nav aria-label="بخش‌های پنل" className="flex flex-col gap-1">
      {NAV.map((item) => {
        const active = isActive(pathname, item.to);
        const Icon = item.icon;
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            onClick={onNavigate}
            className={cn(
              "group relative flex items-center gap-3 rounded-2xl px-3 py-2.5 transition-colors",
              active ? "text-fg" : "text-fg-muted hover:bg-surface-3/60 hover:text-fg",
            )}
          >
            {active ? (
              <motion.span
                layoutId={withDescriptions ? "drawer-active" : "side-active"}
                className="absolute inset-0 rounded-2xl bg-surface shadow-card ring-1 ring-line"
                transition={{ type: "spring", stiffness: 420, damping: 36 }}
              />
            ) : null}
            <span
              className={cn(
                "relative grid size-9 place-items-center rounded-xl transition-colors",
                active ? "bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))] text-white shadow-[0_6px_18px_-8px_var(--ph-brand)]" : "bg-surface-3/70",
              )}
            >
              <Icon className="size-[1.15rem]" />
            </span>
            <span className="relative min-w-0">
              <span className="block text-[0.95rem] font-semibold">{item.label}</span>
              {withDescriptions ? <span className="block truncate text-xs text-fg-faint">{item.description}</span> : null}
            </span>
          </NavLink>
        );
      })}
    </nav>
  );
}

/** Kill switch / dry-run / worker heartbeat, from the live overview. Hidden if it cannot load. */
function FleetPulse() {
  const { data } = useOverview();
  if (!data) return null;
  const age = data.worker_heartbeat_age_s;
  const workerTone = age === null ? "danger" : age > 90 ? "warning" : "success";
  return (
    <div className="flex flex-col gap-2 rounded-2xl border border-line bg-surface-2/70 p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="text-fg-muted">کلید توقف</span>
        <span className={cn("flex items-center gap-1.5 font-bold", data.kill_switch ? "text-danger" : "text-success")}>
          <StatusDot tone={data.kill_switch ? "danger" : "success"} pulse={data.kill_switch} />
          {data.kill_switch ? "روشن" : "خاموش"}
        </span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-fg-muted">حالت کار</span>
        <span className={cn("font-bold", data.dry_run ? "text-warning" : "text-success")}>{data.dry_run ? "خشک (آزمایشی)" : "واقعی"}</span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-fg-muted">ضربان worker</span>
        <span className="flex items-center gap-1.5 font-bold text-fg">
          <StatusDot tone={workerTone} pulse={workerTone === "success"} />
          {age === null ? "ندارد" : formatDurationSeconds(age)}
        </span>
      </div>
    </div>
  );
}

function UserBlock({ onLogout, busy }: { onLogout: () => void; busy: boolean }) {
  const { data: me } = useMe();
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface p-2.5 shadow-card">
      <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-brand-soft text-brand">
        <UserRound className="size-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-fg-faint">مالک پنل</div>
        <div className="ltr truncate text-right text-sm font-bold text-fg">{me?.username ?? "…"}</div>
      </div>
      <button
        type="button"
        onClick={onLogout}
        disabled={busy}
        className="grid size-10 place-items-center rounded-xl text-fg-muted transition-colors hover:bg-danger-soft hover:text-danger disabled:opacity-50"
        aria-label="خروج از پنل"
        title="خروج از پنل"
      >
        <LogOut className="size-5 rtl:-scale-x-100" />
      </button>
    </div>
  );
}

function KillSwitchBanner() {
  const { data } = useOverview();
  const { pathname } = useLocation();
  return (
    <AnimatePresence initial={false}>
      {data?.kill_switch ? (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          className="overflow-hidden"
        >
          <div className="mb-4 flex items-center gap-3 rounded-2xl border border-danger/25 bg-danger-soft px-4 py-3 text-sm text-fg">
            <OctagonPause className="size-5 shrink-0 text-danger" />
            <p className="min-w-0 flex-1 leading-7">
              <b className="text-danger">کلید توقف روشن است.</b> هیچ سروری ساخته یا حذف نمیشود.
            </p>
            {pathname !== "/settings" ? (
              <Link to="/settings" className="shrink-0 rounded-lg px-2 py-1 text-sm font-bold text-danger hover:bg-danger/10">
                مدیریت
              </Link>
            ) : null}
          </div>
        </motion.div>
      ) : data?.dry_run ? (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          className="overflow-hidden"
        >
          <div className="mb-4 flex items-center gap-3 rounded-2xl border border-warning/25 bg-warning-soft px-4 py-3 text-sm text-fg">
            <FlaskConical className="size-5 shrink-0 text-warning" />
            <p className="min-w-0 flex-1 leading-7">
              <b className="text-warning">حالت خشک:</b> چرخه‌ی ناوگان فقط برنامه میریزد و هیچ سروری نمیسازد یا حذف نمیکند.
            </p>
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

/** Keeps the outlet of the route that is leaving, so the exit animation shows the old page. */
function FrozenOutlet() {
  const outlet = useOutlet();
  const [frozen] = useState(outlet);
  return frozen;
}

export function AppShell() {
  const location = useLocation();
  const [drawer, setDrawer] = useState(false);
  const [busy, logout] = useLogout();

  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [location.pathname]);

  return (
    <div className="ph-backdrop min-h-dvh">
      {/* Desktop sidebar (right side: the start edge in RTL) */}
      <aside className="fixed inset-y-0 start-0 z-30 hidden w-72 flex-col gap-5 border-e border-line bg-surface/60 px-4 py-5 backdrop-blur-xl lg:flex">
        <div className="px-2">
          <Logo />
        </div>
        <SideNav />
        <div className="mt-auto flex flex-col gap-3">
          <FleetPulse />
          <div className="flex items-center justify-between px-1">
            <span className="text-xs text-fg-faint">حالت نمایش</span>
            <ThemeSwitch />
          </div>
          <UserBlock onLogout={logout} busy={busy} />
        </div>
      </aside>

      {/* Phone top bar */}
      <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-line bg-bg/75 px-2 pt-[env(safe-area-inset-top)] backdrop-blur-xl lg:hidden">
        <Dialog.Root open={drawer} onOpenChange={setDrawer}>
          <Dialog.Trigger asChild>
            <button type="button" className="grid size-10 place-items-center rounded-xl text-fg hover:bg-surface-3/70" aria-label="منوی پنل">
              <Menu className="size-5" />
            </button>
          </Dialog.Trigger>
          <AnimatePresence>
            {drawer ? (
              <Dialog.Portal forceMount>
                <Dialog.Overlay asChild forceMount>
                  <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="fixed inset-0 z-40 bg-[rgb(6_8_18/0.5)] backdrop-blur-[2px]"
                  />
                </Dialog.Overlay>
                <Dialog.Content asChild forceMount>
                  <motion.div
                    initial={{ x: "100%" }}
                    animate={{ x: 0 }}
                    exit={{ x: "100%" }}
                    transition={{ type: "spring", stiffness: 380, damping: 38 }}
                    className="fixed inset-y-0 right-0 z-50 flex w-[min(20rem,86vw)] flex-col gap-5 border-s border-line bg-surface px-4 pt-[max(1.25rem,env(safe-area-inset-top))] pb-[max(1.25rem,env(safe-area-inset-bottom))] shadow-pop outline-none"
                  >
                    <div className="flex items-center justify-between">
                      <Logo />
                      <Dialog.Close asChild>
                        <button type="button" className="grid size-9 place-items-center rounded-xl text-fg-faint hover:bg-surface-3" aria-label="بستن منو">
                          <X className="size-5" />
                        </button>
                      </Dialog.Close>
                    </div>
                    <Dialog.Title className="sr-only">منوی پنل</Dialog.Title>
                    <Dialog.Description className="sr-only">بخش‌های پنل، حالت نمایش و خروج</Dialog.Description>
                    <SideNav withDescriptions onNavigate={() => setDrawer(false)} />
                    <div className="mt-auto flex flex-col gap-3">
                      <FleetPulse />
                      <div className="flex items-center justify-between px-1">
                        <span className="text-xs text-fg-faint">حالت نمایش</span>
                        <ThemeSwitch />
                      </div>
                      <UserBlock onLogout={logout} busy={busy} />
                    </div>
                  </motion.div>
                </Dialog.Content>
              </Dialog.Portal>
            ) : null}
          </AnimatePresence>
        </Dialog.Root>
        <div className="min-w-0 flex-1 truncate text-center text-[0.98rem] font-bold text-fg">{titleFor(location.pathname)}</div>
        <ThemeToggleButton />
      </header>

      <main className="px-4 pt-5 pb-[calc(6rem+env(safe-area-inset-bottom))] sm:px-6 lg:ms-72 lg:px-10 lg:pt-9 lg:pb-12">
        <div className="mx-auto w-full max-w-7xl">
          <KillSwitchBanner />
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0, transition: { duration: 0.28, ease: [0.16, 1, 0.3, 1] } }}
              exit={{ opacity: 0, y: -6, transition: { duration: 0.12 } }}
            >
              <FrozenOutlet />
            </motion.div>
          </AnimatePresence>
        </div>
      </main>

      {/* Phone bottom navigation */}
      <nav
        aria-label="بخش‌های اصلی"
        className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface/85 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl lg:hidden"
      >
        <ul className="mx-auto grid max-w-lg grid-cols-5">
          {NAV.map((item) => {
            const active = isActive(location.pathname, item.to);
            const Icon = item.icon;
            return (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === "/"}
                  className={cn("relative flex h-16 flex-col items-center justify-center gap-1 text-[0.7rem] font-semibold", active ? "text-brand" : "text-fg-faint")}
                >
                  {active ? (
                    <motion.span
                      layoutId="bottom-active"
                      className="absolute top-1.5 h-8 w-14 rounded-full bg-brand-soft"
                      transition={{ type: "spring", stiffness: 480, damping: 36 }}
                    />
                  ) : null}
                  <Icon className="relative size-5" />
                  <span className="relative">{item.short}</span>
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>
    </div>
  );
}
