import { Archive, ChevronDown } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "../../lib/browser";
import { formatNumber } from "../../lib/format";

/**
 * «پایان‌یافته‌ها»: a collapsed section under a list, closed by default, with how many items it
 * holds. Nothing is shown when there are none. A native <details>, so it opens with the keyboard
 * and a screen reader announces it as expandable.
 */
export function EndedSection({ count, hint, children, className }: { count: number; hint?: string; children: ReactNode; className?: string }) {
  if (count <= 0) return null;
  return (
    <details className={cn("group rounded-3xl border border-line bg-surface-2/40", className)}>
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-3xl px-4 py-3 text-sm font-semibold text-fg-muted select-none hover:text-fg [&::-webkit-details-marker]:hidden">
        <Archive className="size-4 shrink-0" aria-hidden />
        <span>پایان‌یافته‌ها</span>
        <span className="num rounded-full bg-surface-3 px-2 py-0.5 text-xs text-fg" aria-label={`${formatNumber(count)} مورد`}>
          {formatNumber(count)}
        </span>
        {hint ? <span className="hidden truncate text-xs font-normal text-fg-faint sm:inline">{hint}</span> : null}
        <ChevronDown className="ms-auto size-4 shrink-0 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <div className="px-3 pb-3 sm:px-4 sm:pb-4">{children}</div>
    </details>
  );
}
