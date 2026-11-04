import type { Tone } from "./labels";

/** Tailwind classes per semantic tone (written out in full so Tailwind can see them). */
export const TONE_SOFT: Record<Tone, string> = {
  neutral: "bg-surface-3 text-fg-muted",
  brand: "bg-brand-soft text-brand",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
};

export const TONE_DOT: Record<Tone, string> = {
  neutral: "bg-fg-faint",
  brand: "bg-brand",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
};

export const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-fg-muted",
  brand: "text-brand",
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
  info: "text-info",
};

/** CSS variable of each tone, for charts and inline gradients. */
export const TONE_VAR: Record<Tone, string> = {
  neutral: "var(--ph-fg-faint)",
  brand: "var(--ph-brand)",
  success: "var(--ph-success)",
  warning: "var(--ph-warning)",
  danger: "var(--ph-danger)",
  info: "var(--ph-info)",
};
