import { Ban, ChevronDown, CircleCheck, FlaskConical, Info } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";

import { cn } from "../lib/browser";
import { resultText, verdictOf } from "../lib/events";
import { formatJalali, formatRelative, toFaDigits } from "../lib/format";
import { actionLabel, actorLabel, reasonLabel } from "../lib/labels";
import type { AuditEvent } from "../lib/types";
import { Badge } from "./ui/Card";

const VERDICT = {
  allowed: { icon: CircleCheck, cls: "bg-series-1/12 text-series-1", label: "مجاز" },
  blocked: { icon: Ban, cls: "bg-series-2/12 text-series-2", label: "ردشده" },
  info: { icon: Info, cls: "bg-series-3/12 text-series-3", label: "گزارشی" },
} as const;

export function EventRow({ event, compact = false }: { event: AuditEvent; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const v = VERDICT[verdictOf(event)];
  const Icon = v.icon;
  const reasons = event.reasons ?? [];
  const result = resultText(event.result);
  const expandable = !compact && (reasons.length > 0 || result !== "" || event.target !== null);

  return (
    <li className="group">
      <button
        type="button"
        disabled={!expandable}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={expandable ? open : undefined}
        className={cn(
          "flex w-full items-start gap-3 rounded-2xl px-3 py-3 text-start transition-colors",
          expandable && "hover:bg-surface-2",
        )}
      >
        <span className={cn("mt-0.5 grid size-9 shrink-0 place-items-center rounded-xl", v.cls)} title={v.label}>
          <Icon className="size-[1.1rem]" aria-hidden />
          <span className="sr-only">{v.label}</span>
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-[0.93rem] font-bold text-fg">{actionLabel(event.action)}</span>
            {event.dry_run ? (
              <Badge tone="warning" className="gap-1 px-2 py-0.5">
                <FlaskConical className="size-3" aria-hidden />
                خشک
              </Badge>
            ) : null}
            {verdictOf(event) === "blocked" ? (
              <Badge tone="danger" className="px-2 py-0.5">
                رد شد
              </Badge>
            ) : null}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs leading-6 text-fg-muted">
            <span>{actorLabel(event.actor)}</span>
            {event.target ? (
              <>
                <span aria-hidden>·</span>
                <span className="ltr max-w-[14rem] truncate font-mono text-[0.72rem]">{event.target}</span>
              </>
            ) : null}
          </span>
          {!compact && reasons.length > 0 && !open ? (
            <span className="mt-1 line-clamp-1 text-xs text-fg-faint">{reasons.map(reasonLabel).join("، ")}</span>
          ) : null}
        </span>
        <span className="flex shrink-0 flex-col items-end gap-1 text-left">
          <time dateTime={event.at} title={formatJalali(event.at, "long")} className="text-xs whitespace-nowrap text-fg-faint">
            {formatRelative(event.at)}
          </time>
          {expandable ? (
            <ChevronDown className={cn("size-4 text-fg-faint transition-transform", open && "rotate-180")} aria-hidden />
          ) : null}
        </span>
      </button>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <dl className="mx-3 mb-3 grid gap-x-4 gap-y-2 rounded-2xl border border-line bg-surface-2 p-3.5 text-sm sm:grid-cols-[8rem_1fr]">
              <dt className="text-fg-faint">زمان</dt>
              <dd className="text-fg">{formatJalali(event.at, "long")}</dd>
              <dt className="text-fg-faint">شناسه</dt>
              <dd className="text-fg">{toFaDigits(event.id)}</dd>
              <dt className="text-fg-faint">کد کار</dt>
              <dd className="ltr text-right font-mono text-xs text-fg">{event.action}</dd>
              {event.target ? (
                <>
                  <dt className="text-fg-faint">هدف</dt>
                  <dd className="ltr text-right font-mono text-xs break-all text-fg">{event.target}</dd>
                </>
              ) : null}
              {reasons.length > 0 ? (
                <>
                  <dt className="text-fg-faint">دلیل‌ها</dt>
                  <dd className="flex flex-wrap gap-1.5">
                    {reasons.map((r) => (
                      <span key={r} className="rounded-lg bg-surface-3 px-2 py-0.5 text-xs text-fg" title={r}>
                        {reasonLabel(r)}
                      </span>
                    ))}
                  </dd>
                </>
              ) : null}
              {result ? (
                <>
                  <dt className="text-fg-faint">نتیجه</dt>
                  <dd className="ltr text-right font-mono text-xs break-all text-fg">{result}</dd>
                </>
              ) : null}
            </dl>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </li>
  );
}
