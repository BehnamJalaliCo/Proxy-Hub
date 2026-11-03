import { LoaderCircle } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Link } from "react-router";

import { cn } from "../../lib/browser";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "success";
type Size = "sm" | "md" | "lg";

const VARIANT: Record<Variant, string> = {
  primary:
    "text-on-brand bg-[linear-gradient(110deg,var(--ph-brand),var(--ph-brand-strong)_55%,var(--ph-brand-2))] shadow-[0_8px_24px_-10px_var(--ph-brand)] hover:brightness-110 active:brightness-95",
  secondary: "bg-surface text-fg border border-line-strong hover:bg-surface-2 active:bg-surface-3",
  ghost: "text-fg-muted hover:text-fg hover:bg-surface-3/70",
  danger: "text-white bg-danger shadow-[0_8px_22px_-12px_var(--ph-danger)] hover:brightness-110 active:brightness-95",
  success: "text-white bg-success shadow-[0_8px_22px_-12px_var(--ph-success)] hover:brightness-110",
};

const SIZE: Record<Size, string> = {
  sm: "h-9 px-3 text-sm gap-1.5 rounded-xl",
  md: "h-11 px-4 text-[0.95rem] gap-2 rounded-xl",
  lg: "h-12 px-5 text-base gap-2 rounded-2xl",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
  block?: boolean;
}

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  icon,
  block = false,
  className,
  children,
  disabled,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled === true || loading}
      aria-busy={loading || undefined}
      className={cn(
        "inline-flex select-none items-center justify-center font-semibold whitespace-nowrap transition-[filter,background-color,color,transform,box-shadow] duration-150 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-55",
        VARIANT[variant],
        SIZE[size],
        block && "w-full",
        className,
      )}
      {...rest}
    >
      {loading ? <LoaderCircle className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
}

/** A router link that looks like a button (never a <button> inside an <a>). */
export function LinkButton({
  to,
  variant = "primary",
  size = "md",
  icon,
  block = false,
  className,
  children,
}: {
  to: string;
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
  block?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link
      to={to}
      className={cn(
        "inline-flex select-none items-center justify-center font-semibold whitespace-nowrap transition-[filter,background-color,color,transform] duration-150 active:scale-[0.98]",
        VARIANT[variant],
        SIZE[size],
        block && "w-full",
        className,
      )}
    >
      {icon}
      {children}
    </Link>
  );
}

export function IconButton({
  label,
  className,
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex size-10 items-center justify-center rounded-xl text-fg-muted transition-colors hover:bg-surface-3/70 hover:text-fg active:scale-95",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
