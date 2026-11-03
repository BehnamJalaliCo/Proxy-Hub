import { Eye, EyeOff, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Switch as RSwitch } from "radix-ui";
import { forwardRef, useId, useState, type InputHTMLAttributes, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "../../lib/browser";

export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
  className,
  trailing,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null | undefined;
  htmlFor: string;
  children: ReactNode;
  className?: string;
  trailing?: ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={htmlFor} className="text-sm font-semibold text-fg">
          {label}
        </label>
        {trailing}
      </div>
      {children}
      <AnimatePresence initial={false} mode="wait">
        {error ? (
          <motion.p
            key="error"
            id={`${htmlFor}-error`}
            role="alert"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="text-[0.82rem] leading-6 text-danger"
          >
            {error}
          </motion.p>
        ) : hint ? (
          <motion.p
            key="hint"
            id={`${htmlFor}-hint`}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="text-[0.82rem] leading-6 text-fg-faint"
          >
            {hint}
          </motion.p>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

const INPUT =
  "h-12 w-full rounded-xl border bg-surface-2 px-3.5 text-base text-fg placeholder:text-fg-faint transition-[border-color,box-shadow,background-color] outline-none focus:border-brand focus:bg-surface focus:ring-4 focus:ring-[var(--ph-ring)] disabled:opacity-60";

export interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
  /** Latin content (usernames, codes, tokens) typed left-to-right inside the RTL page. */
  latin?: boolean;
  suffix?: ReactNode;
}

export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
  { invalid = false, latin = false, suffix, className, ...rest },
  ref,
) {
  const input = (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      dir={latin ? "ltr" : undefined}
      className={cn(
        INPUT,
        invalid ? "border-danger" : "border-line",
        latin && "font-medium tracking-wide",
        latin && !className?.includes("text-center") && "text-left",
        suffix ? "pl-12" : "",
        className,
      )}
      {...rest}
    />
  );
  if (!suffix) return input;
  return (
    <div className="relative">
      {input}
      <span className="pointer-events-none absolute inset-y-0 left-3.5 flex items-center text-sm text-fg-faint">{suffix}</span>
    </div>
  );
});

/** Password-style input with a show/hide toggle. Never pre-filled from server data. */
export const SecretInput = forwardRef<HTMLInputElement, TextInputProps & { allowReveal?: boolean }>(function SecretInput(
  { invalid = false, allowReveal = true, className, ...rest },
  ref,
) {
  const [shown, setShown] = useState(false);
  return (
    <div className="relative">
      <input
        ref={ref}
        type={shown ? "text" : "password"}
        dir="ltr"
        spellCheck={false}
        autoCapitalize="none"
        autoCorrect="off"
        aria-invalid={invalid || undefined}
        className={cn(INPUT, allowReveal ? "pl-12 text-left" : "text-left", invalid ? "border-danger" : "border-line", className)}
        {...rest}
      />
      {allowReveal ? (
        <button
          type="button"
          onClick={() => setShown((v) => !v)}
          className="absolute inset-y-0 left-1 my-auto grid size-10 place-items-center rounded-lg text-fg-faint hover:text-fg"
          aria-label={shown ? "پنهان کردن" : "نمایش"}
          title={shown ? "پنهان کردن" : "نمایش"}
        >
          {shown ? <EyeOff className="size-[1.1rem]" /> : <Eye className="size-[1.1rem]" />}
        </button>
      ) : null}
    </div>
  );
});

export function Toggle({
  checked,
  onCheckedChange,
  label,
  description,
  disabled = false,
  tone = "brand",
  id,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  label: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  tone?: "brand" | "danger" | "warning";
  id?: string;
}) {
  const auto = useId();
  const switchId = id ?? auto;
  const on = { brand: "data-[state=checked]:bg-brand", danger: "data-[state=checked]:bg-danger", warning: "data-[state=checked]:bg-warning" }[tone];
  return (
    <div className="flex items-start justify-between gap-4">
      <label htmlFor={switchId} className="min-w-0 flex-1 cursor-pointer">
        <span className="block text-sm font-semibold text-fg">{label}</span>
        {description ? <span className="mt-0.5 block text-[0.82rem] leading-6 text-fg-muted">{description}</span> : null}
      </label>
      <RSwitch.Root
        id={switchId}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        className={cn(
          "relative mt-0.5 inline-flex h-7 w-12 shrink-0 items-center rounded-full bg-surface-3 shadow-inner ring-1 ring-line transition-colors disabled:opacity-50",
          on,
        )}
      >
        <RSwitch.Thumb className="block size-6 translate-x-[-2px] rounded-full bg-white shadow-md transition-transform duration-200 will-change-transform data-[state=checked]:translate-x-[-22px]" />
      </RSwitch.Root>
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
  label: string;
  className?: string;
}) {
  const group = useId();
  return (
    <div role="radiogroup" aria-label={label} className={cn("inline-flex rounded-xl bg-surface-3 p-1", className)}>
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(o.value)}
            className={cn(
              "relative h-8 flex-1 rounded-lg px-3 text-[0.82rem] font-semibold whitespace-nowrap transition-colors",
              active ? "text-fg" : "text-fg-muted hover:text-fg",
            )}
          >
            {active ? (
              <motion.span
                layoutId={`seg-${group}`}
                className="absolute inset-0 rounded-lg bg-surface shadow-sm ring-1 ring-line"
                transition={{ type: "spring", stiffness: 500, damping: 38 }}
              />
            ) : null}
            <span className="relative">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Editable list of short Latin tokens (server types, locations) with suggestions. */
export function TagInput({
  id,
  values,
  onChange,
  suggestions = [],
  placeholder,
  invalid = false,
  normalize = (s: string) => s.trim().toLowerCase(),
}: {
  id: string;
  values: string[];
  onChange: (v: string[]) => void;
  suggestions?: string[];
  placeholder?: string;
  invalid?: boolean;
  normalize?: (s: string) => string;
}) {
  const [text, setText] = useState("");
  const add = (raw: string) => {
    const v = normalize(raw);
    if (v === "" || values.includes(v)) {
      setText("");
      return;
    }
    onChange([...values, v]);
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === "," || e.key === "،") {
      e.preventDefault();
      add(text);
    } else if (e.key === "Backspace" && text === "" && values.length > 0) {
      onChange(values.slice(0, -1));
    }
  };
  const rest = suggestions.filter((s) => !values.includes(s));
  return (
    <div className="flex flex-col gap-2">
      <div
        className={cn(
          "flex min-h-12 flex-wrap items-center gap-1.5 rounded-xl border bg-surface-2 p-1.5 transition-[border-color,box-shadow] focus-within:border-brand focus-within:ring-4 focus-within:ring-[var(--ph-ring)]",
          invalid ? "border-danger" : "border-line",
        )}
      >
        <AnimatePresence initial={false}>
          {values.map((v) => (
            <motion.span
              key={v}
              layout
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.8 }}
              className="inline-flex h-8 items-center gap-1 rounded-lg bg-brand-soft ps-2.5 pe-1 text-sm font-semibold text-brand"
            >
              <span className="ltr">{v}</span>
              <button
                type="button"
                onClick={() => onChange(values.filter((x) => x !== v))}
                className="grid size-6 place-items-center rounded-md hover:bg-brand/15"
                aria-label={`حذف ${v}`}
              >
                <X className="size-3.5" />
              </button>
            </motion.span>
          ))}
        </AnimatePresence>
        <input
          id={id}
          value={text}
          dir="ltr"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          onBlur={() => text && add(text)}
          placeholder={values.length === 0 ? placeholder : undefined}
          spellCheck={false}
          autoCapitalize="none"
          className="h-8 min-w-24 flex-1 bg-transparent px-2 text-left text-base text-fg sm:text-sm outline-none placeholder:text-fg-faint"
        />
      </div>
      {rest.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {rest.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => add(s)}
              className="ltr h-7 rounded-lg border border-dashed border-line-strong px-2.5 text-xs font-semibold text-fg-muted transition-colors hover:border-brand hover:text-brand"
            >
              + {s}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const FA = (n: number) => String(n).padStart(2, "0").replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)] ?? d);
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, "0"));
const MINUTE_STEP = 5;

/** A 24-hour time of day ("HH:MM") as two native selects: always valid, the same on every phone
 * (a browser <input type="time"> may show 12-hour AM/PM in a Persian locale). Hour on the left,
 * as a clock reads. A minute off the 5-minute grid (set elsewhere) stays selectable. */
export function TimeSelect({ value, onChange, label, invalid = false }: { value: string; onChange: (v: string) => void; label: string; invalid?: boolean }) {
  const [h = "00", m = "00"] = value.split(":");
  const minutes = Array.from({ length: 60 / MINUTE_STEP }, (_, i) => String(i * MINUTE_STEP).padStart(2, "0"));
  if (!minutes.includes(m)) minutes.push(m);
  minutes.sort();
  const cls = cn(
    "h-11 appearance-none rounded-xl border bg-surface px-3 text-center text-base font-semibold text-fg outline-none focus:border-brand focus:ring-4 focus:ring-[var(--ph-ring)]",
    invalid ? "border-danger" : "border-line",
  );
  return (
    <div role="group" aria-label={label} dir="ltr" className="flex items-center gap-1">
      <select aria-label={`${label}: ساعت`} value={h} onChange={(e) => onChange(`${e.target.value}:${m}`)} className={cls}>
        {HOUR_OPTIONS.map((x) => (
          <option key={x} value={x}>
            {FA(Number(x))}
          </option>
        ))}
      </select>
      <span className="font-bold text-fg-faint">:</span>
      <select aria-label={`${label}: دقیقه`} value={m} onChange={(e) => onChange(`${h}:${e.target.value}`)} className={cls}>
        {minutes.map((x) => (
          <option key={x} value={x}>
            {FA(Number(x))}
          </option>
        ))}
      </select>
    </div>
  );
}
