import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "../../lib/browser";
import type { Tone } from "../../lib/labels";
import { TONE_DOT, TONE_SOFT } from "../../lib/tones";

export function Card({ className, children, ...rest }: HTMLAttributes<HTMLElement>) {
  return (
    <section className={cn("rounded-3xl border border-line bg-surface shadow-card", className)} {...rest}>
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  subtitle,
  icon,
  action,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("flex items-start gap-3 px-5 pt-5", className)}>
      {icon ? (
        <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">{icon}</span>
      ) : null}
      <div className="min-w-0 flex-1">
        <h2 className="text-[1.02rem] leading-7 font-bold text-fg">{title}</h2>
        {subtitle ? <p className="text-sm leading-6 text-fg-muted">{subtitle}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </header>
  );
}

export function Badge({
  tone = "neutral",
  dot = false,
  pulse = false,
  className,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  pulse?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs leading-none font-semibold whitespace-nowrap",
        TONE_SOFT[tone],
        className,
      )}
    >
      {dot ? <StatusDot tone={tone} pulse={pulse} /> : null}
      {children}
    </span>
  );
}

export function StatusDot({ tone, pulse = false, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return (
    <span className={cn("relative inline-flex size-2", className)} aria-hidden>
      {pulse ? <span className={cn("absolute inset-0 animate-ping-soft rounded-full", TONE_DOT[tone])} /> : null}
      <span className={cn("relative inline-flex size-2 rounded-full", TONE_DOT[tone])} />
    </span>
  );
}
