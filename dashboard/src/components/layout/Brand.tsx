import { Monitor, Moon, Sun } from "lucide-react";
import { motion } from "motion/react";
import { useId } from "react";

import { cn } from "../../lib/browser";
import { setTheme, useTheme, type ThemeChoice } from "../../lib/theme";

export function LogoMark({ className }: { className?: string }) {
  const gid = `ph-logo-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <svg viewBox="0 0 64 64" className={cn("size-10", className)} aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#7c6cff" />
          <stop offset="1" stopColor="#22c3e6" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="18" fill={`url(#${gid})`} />
      <g stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeOpacity=".85">
        <path d="M19 21l9 8M45 21l-9 8M19 43l9-8M45 43l-9-8" />
      </g>
      <circle cx="32" cy="32" r="7" fill="#fff" />
      <circle cx="16" cy="18" r="4.5" fill="#fff" fillOpacity=".9" />
      <circle cx="48" cy="18" r="4.5" fill="#fff" fillOpacity=".9" />
      <circle cx="16" cy="46" r="4.5" fill="#fff" fillOpacity=".9" />
      <circle cx="48" cy="46" r="4.5" fill="#fff" fillOpacity=".9" />
    </svg>
  );
}

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <LogoMark className={compact ? "size-9" : "size-11"} />
      <div className="leading-tight">
        <div className="ltr text-[1.05rem] font-extrabold tracking-tight text-fg">Proxy-Hub</div>
        {!compact ? <div className="text-xs text-fg-muted">پنل خودکار پروکسی</div> : null}
      </div>
    </div>
  );
}

const CHOICES: { value: ThemeChoice; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "روشن", icon: Sun },
  { value: "dark", label: "تاریک", icon: Moon },
  { value: "system", label: "خودکار", icon: Monitor },
];

/** Three-way theme switch (light / dark / follow the device). */
export function ThemeSwitch({ className }: { className?: string }) {
  const { choice } = useTheme();
  return (
    <div role="radiogroup" aria-label="حالت نمایش" className={cn("inline-flex rounded-xl bg-surface-3 p-1", className)}>
      {CHOICES.map((c) => {
        const active = c.value === choice;
        const Icon = c.icon;
        return (
          <button
            key={c.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={c.label}
            title={c.label}
            onClick={() => setTheme(c.value)}
            className={cn(
              "relative grid h-8 w-9 place-items-center rounded-lg transition-colors",
              active ? "text-brand" : "text-fg-faint hover:text-fg",
            )}
          >
            {active ? (
              <motion.span
                layoutId="theme-pill"
                className="absolute inset-0 rounded-lg bg-surface shadow-sm ring-1 ring-line"
                transition={{ type: "spring", stiffness: 500, damping: 36 }}
              />
            ) : null}
            <Icon className="relative size-4" />
          </button>
        );
      })}
    </div>
  );
}

/** One-tap light/dark toggle for small top bars. */
export function ThemeToggleButton() {
  const { resolved } = useTheme();
  const next = resolved === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={next === "dark" ? "حالت تاریک" : "حالت روشن"}
      title={next === "dark" ? "حالت تاریک" : "حالت روشن"}
      className="grid size-10 place-items-center rounded-xl text-fg-muted transition-colors hover:bg-surface-3/70 hover:text-fg"
    >
      <motion.span
        key={resolved}
        initial={{ rotate: -90, opacity: 0, scale: 0.6 }}
        animate={{ rotate: 0, opacity: 1, scale: 1 }}
        transition={{ type: "spring", stiffness: 300, damping: 18 }}
      >
        {resolved === "dark" ? <Moon className="size-5" /> : <Sun className="size-5" />}
      </motion.span>
    </button>
  );
}
