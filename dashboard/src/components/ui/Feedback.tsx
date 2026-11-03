import { Check, Copy, RefreshCw, TriangleAlert } from "lucide-react";
import { animate, motion, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { errorMessage, isApiError } from "../../lib/api";
import { cn, copyText } from "../../lib/browser";
import { toast } from "../../lib/toast";
import { Button } from "./Button";

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("ph-skeleton rounded-xl", className)} aria-hidden />;
}

/** Placeholder card body while data loads. */
export function SkeletonLines({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2.5", className)} role="status" aria-label="در حال بارگذاری">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className={cn("h-4", i === lines - 1 ? "w-2/3" : "w-full")} />
      ))}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-10 text-center", className)}>
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ type: "spring", stiffness: 260, damping: 20 }}
        className="grid size-16 place-items-center rounded-3xl bg-brand-soft text-brand ring-8 ring-brand-soft/40"
      >
        {icon}
      </motion.div>
      <h3 className="text-base font-bold text-fg">{title}</h3>
      {description ? <p className="max-w-md text-sm leading-7 text-fg-muted">{description}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  title = "این بخش بارگذاری نشد",
  className,
  compact = false,
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  className?: string;
  compact?: boolean;
}) {
  const code = isApiError(error) ? error.status : 0;
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center gap-3 text-center",
        compact ? "px-4 py-6" : "px-6 py-10",
        className,
      )}
    >
      <div className="grid size-14 place-items-center rounded-3xl bg-danger-soft text-danger">
        <TriangleAlert className="size-6" />
      </div>
      <h3 className="text-base font-bold text-fg">{title}</h3>
      <p className="max-w-md text-sm leading-7 text-fg-muted">{errorMessage(error)}</p>
      {code > 0 ? <p className="ltr text-xs text-fg-faint">HTTP {code}</p> : null}
      {onRetry ? (
        <Button variant="secondary" size="sm" icon={<RefreshCw className="size-4" />} onClick={onRetry}>
          تلاش دوباره
        </Button>
      ) : null}
    </div>
  );
}

/** Counts up to `value` (eases between values); the text is always the final formatted value for
 * screen readers. */
export function AnimatedNumber({
  value,
  format,
  className,
}: {
  value: number;
  format: (n: number) => string;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const from = useRef(0);
  const reduce = useReducedMotion();
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (reduce) {
      el.textContent = format(value);
      from.current = value;
      return;
    }
    const controls = animate(from.current, value, {
      duration: 0.9,
      ease: [0.16, 1, 0.3, 1],
      onUpdate: (v) => {
        el.textContent = format(v);
      },
    });
    from.current = value;
    return () => controls.stop();
  }, [value, format, reduce]);
  return (
    <span className={cn("num", className)} aria-label={format(value)}>
      <span ref={ref} aria-hidden>
        {format(reduce ? value : 0)}
      </span>
    </span>
  );
}

export function CopyButton({
  text,
  label = "کپی",
  doneLabel = "کپی شد",
  className,
  size = "sm",
}: {
  text: string;
  label?: string;
  doneLabel?: string;
  className?: string;
  size?: "sm" | "md";
}) {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return;
    const t = window.setTimeout(() => setDone(false), 1800);
    return () => window.clearTimeout(t);
  }, [done]);
  return (
    <Button
      variant="secondary"
      size={size}
      className={className}
      icon={done ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}
      onClick={() => {
        void copyText(text).then((okay) => {
          if (okay) setDone(true);
          else toast.error("کپی نشد", "متن را دستی انتخاب و کپی کنید.");
        });
      }}
    >
      {done ? doneLabel : label}
    </Button>
  );
}
