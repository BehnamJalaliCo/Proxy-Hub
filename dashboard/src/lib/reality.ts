// VLESS Reality (decision D18): the shape of GET /api/reality and pure, unit-tested helpers for the
// Reality page (remaining time and volume, the words for each state, the settings form). The server
// is always the authority; these only mirror its rules (panel/proxyhub/reality/settings.py).
// Nothing here is about a person: a config is a link anyone in the channel may use.

import { formatNumber } from "./format";
import type { Tone } from "./labels";
import { normalizeDigits } from "./validation";

export type RealityStatus = "active" | "expired" | "full" | "burned";

export interface RealitySettings {
  enabled: boolean;
  configs_per_node: number;
  cap_hours: number;
  cap_gb: number;
  target_host: string;
  target_port: number;
  link_name: string | null;
}

export interface RealitySettingsView extends RealitySettings {
  /** Fixed by the cross-team contract: Xray listens on TCP 443 on every node. */
  port: number;
  /** Bytes per GB the caps use (decimal, 10^9). */
  gb_bytes: number;
}

export interface RealityXray {
  enabled: boolean;
  up: boolean;
  version: string | null;
  uptime_s: number | null;
  target_ok: boolean | null;
  apply_error: string | null;
  reported_at: string | null;
}

export interface RealityNode {
  node_id: string;
  name: string;
  slot: number | null;
  location: string;
  state: string;
  /** null: this node's agent has never reported Xray (an older agent or image). */
  xray: RealityXray | null;
  public_key: string | null;
  target: string | null;
  active_configs: number;
}

export interface RealityConfig {
  id: number;
  key: string;
  name: string;
  slot: number | null;
  node_name: string;
  location: string;
  host: string;
  status: RealityStatus;
  /** Handed to posts (its node confirmed it is serving). */
  listed: boolean;
  created_at: string;
  expires_at: string;
  ended_at: string | null;
  remaining_s: number;
  data_cap_bytes: number;
  used_bytes: number;
  remaining_bytes: number;
  /** The vless:// link (active configs only): the owner publishes it on purpose. */
  url: string | null;
}

export interface RealitySummary {
  settings: RealitySettingsView;
  defaults: RealitySettingsView;
  totals: {
    active: number;
    expired: number;
    full: number;
    burned: number;
    used_bytes: number;
    nodes_enabled: number;
    nodes_up: number;
  };
  nodes: RealityNode[];
  configs: RealityConfig[];
  generated_at: string;
}

// --- words ---------------------------------------------------------------------------------------

export const REALITY_STATUS: Record<RealityStatus, { label: string; tone: Tone; hint: string }> = {
  active: { label: "فعال", tone: "success", hint: "در پست‌ها قابل استفاده است." },
  expired: { label: "زمانش تمام شد", tone: "neutral", hint: "مهلت کانفیگ گذشت (⌛ روی پست)." },
  full: { label: "حجمش پر شد", tone: "warning", hint: "سقف حجم کانفیگ مصرف شد (🔴 روی پست)." },
  burned: { label: "از دسترس خارج شد", tone: "danger", hint: "سرور یا دامنه‌اش دیگر کار نمیکند (🔴 روی پست)." },
};

const APPLY_ERRORS: Record<string, string> = {
  xray_config_refused: "Xray تنظیمات تازه را نپذیرفت",
  xray_service_failed: "Xray روی node بالا نیامد",
  xray_write_failed: "نوشتن تنظیمات روی node نشد",
  xray_render_refused: "تنظیمات با قفل‌های حریم کاربر جور نبود",
  xray_invariant_refused: "تنظیمات با قفل‌های حریم کاربر جور نبود",
  xray_read_failed: "خواندن تنظیمات روی node نشد",
};

/** The Xray state of a node in one Persian phrase and a tone. */
export function xrayState(x: RealityXray | null): { label: string; tone: Tone } {
  if (x === null) return { label: "عامل قدیمی؛ Xray ندارد", tone: "neutral" };
  if (!x.enabled) return { label: "Xray روی این node نصب نیست", tone: "neutral" };
  if (x.apply_error) return { label: APPLY_ERRORS[x.apply_error] ?? "خطا در اعمال تنظیمات Xray", tone: "danger" };
  if (x.target_ok === false) return { label: "سایت هدف از این node جواب درست نداد", tone: "danger" };
  if (!x.up) return { label: "Xray هنوز بالا نیامده", tone: "warning" };
  return { label: "Xray فعال است", tone: "success" };
}

// --- numbers -------------------------------------------------------------------------------------

/** Bytes in decimal units with Persian digits: "۴۸٫۲ گیگ", "۳۵۰ مگ", "۱٫۲ ترا". */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units: [number, string][] = [
    [1e12, "ترا"],
    [1e9, "گیگ"],
    [1e6, "مگ"],
    [1e3, "کیلوبایت"],
  ];
  for (const [size, name] of units) {
    if (bytes >= size) return `${formatNumber(bytes / size, bytes >= size * 100 ? 0 : 1)} ${name}`;
  }
  return `${formatNumber(bytes)} بایت`;
}

/** Share of a config already used (0..1) in time and in volume; the larger one ends it first. */
export function usage(c: Pick<RealityConfig, "created_at" | "expires_at" | "used_bytes" | "data_cap_bytes">, now: Date): {
  time: number;
  volume: number;
} {
  const start = new Date(c.created_at).getTime();
  const end = new Date(c.expires_at).getTime();
  const span = end - start;
  const time = span > 0 ? Math.min(1, Math.max(0, (now.getTime() - start) / span)) : 1;
  const volume = c.data_cap_bytes > 0 ? Math.min(1, Math.max(0, c.used_bytes / c.data_cap_bytes)) : 1;
  return { time, volume };
}

/** Tone of a meter: calm until most is used. */
export function meterTone(ratio: number): Tone {
  if (ratio >= 0.9) return "danger";
  if (ratio >= 0.7) return "warning";
  return "success";
}

/** Active configs first (soonest to end first), then ended ones (newest first). */
export function sortConfigs(configs: RealityConfig[]): RealityConfig[] {
  return [...configs].sort((a, b) => {
    const aa = a.status === "active" ? 0 : 1;
    const bb = b.status === "active" ? 0 : 1;
    if (aa !== bb) return aa - bb;
    if (aa === 0) return a.expires_at.localeCompare(b.expires_at) || a.id - b.id;
    return (b.ended_at ?? "").localeCompare(a.ended_at ?? "") || b.id - a.id;
  });
}

// --- the settings form ---------------------------------------------------------------------------

export interface RealityDraft {
  enabled: boolean;
  configs_per_node: string;
  cap_hours: string;
  cap_gb: string;
  target_host: string;
  target_port: string;
  link_name: string;
}

export type RealityDraftErrors = Partial<Record<keyof RealityDraft, string>>;

const HOST = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/;
const LINK_NAME = /^[A-Za-z0-9_][A-Za-z0-9_ .|-]{0,31}$/;

export function draftFrom(s: RealitySettings): RealityDraft {
  return {
    enabled: s.enabled,
    configs_per_node: String(s.configs_per_node),
    cap_hours: String(s.cap_hours),
    cap_gb: String(s.cap_gb),
    target_host: s.target_host,
    target_port: String(s.target_port),
    link_name: s.link_name ?? "",
  };
}

function intIn(raw: string, min: number, max: number): number | string {
  const s = normalizeDigits(raw);
  if (!/^\d{1,6}$/.test(s)) return "فقط عدد صحیح وارد کنید.";
  const n = Number(s);
  if (n < min || n > max) return `عددی بین ${formatNumber(min)} و ${formatNumber(max)} وارد کنید.`;
  return n;
}

/** Draft -> settings to send, or the Persian problem of each field. */
export function parseDraft(d: RealityDraft): { settings: RealitySettings | null; errors: RealityDraftErrors } {
  const errors: RealityDraftErrors = {};
  const num = (k: "configs_per_node" | "cap_hours" | "cap_gb" | "target_port", min: number, max: number): number => {
    const v = intIn(d[k], min, max);
    if (typeof v === "string") {
      errors[k] = v;
      return min;
    }
    return v;
  };
  const configs = num("configs_per_node", 1, 20);
  const hours = num("cap_hours", 1, 24 * 30);
  const gb = num("cap_gb", 1, 10_000);
  const port = num("target_port", 1, 65535);
  const host = d.target_host.trim().toLowerCase();
  if (!HOST.test(host) || /^[\d.]+$/.test(host)) errors.target_host = "نام دامنه‌ی سایت هدف را بنویسید، مثل dl.google.com (نه IP).";
  const name = d.link_name.trim().replace(/^@/, "");
  if (name !== "" && !LINK_NAME.test(name)) errors.link_name = "۱ تا ۳۲ حرف انگلیسی، رقم، فاصله یا _ . | -";
  if (Object.keys(errors).length > 0) return { settings: null, errors };
  return {
    settings: {
      enabled: d.enabled,
      configs_per_node: configs,
      cap_hours: hours,
      cap_gb: gb,
      target_host: host,
      target_port: port,
      link_name: name === "" ? null : name,
    },
    errors,
  };
}

export function sameSettings(a: RealitySettings, b: RealitySettings): boolean {
  return (
    a.enabled === b.enabled &&
    a.configs_per_node === b.configs_per_node &&
    a.cap_hours === b.cap_hours &&
    a.cap_gb === b.cap_gb &&
    a.target_host === b.target_host &&
    a.target_port === b.target_port &&
    a.link_name === b.link_name
  );
}

/** "every N hours": how often a node gets a fresh config with these settings. */
export function freshEveryHours(s: Pick<RealitySettings, "cap_hours" | "configs_per_node">): number {
  return s.cap_hours / Math.max(1, s.configs_per_node);
}
