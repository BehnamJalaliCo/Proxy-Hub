// VLESS configs (decision D18; transport ws_tls since 2026-11-05): the shape of GET /api/reality and
// pure, unit-tested helpers for the Reality page (remaining time and volume, the words for each state,
// the transport, the settings form). The server is always the authority; these only mirror its rules
// (panel/proxyhub/reality/settings.py). Nothing here is about a person: a config is a link anyone in
// the channel may use.

import { formatNumber, toFaDigits } from "./format";
import type { Tone } from "./labels";
import { normalizeDigits } from "./validation";

export type RealityStatus = "active" | "expired" | "full" | "burned";

/** ws_tls: VLESS + WebSocket + TLS with the panel's certificate for the node's own name (default).
 * reality: VLESS + REALITY with a foreign target site. */
export type Transport = "ws_tls" | "reality";

export interface RealitySettings {
  enabled: boolean;
  transport: Transport;
  /** With transport reality: the same configs also through VLESS + XHTTP + REALITY on its own port
   * (each config gets a second link). */
  xhttp_enabled: boolean;
  /** D41: a working config gets another period (same link) an hour before its end. */
  renew: boolean;
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
  /** Fixed as well: the XHTTP inbound's port (2083). */
  xhttp_port: number;
  /** Bytes per GB the caps use (decimal, 10^9). */
  gb_bytes: number;
}

export interface RealityXray {
  enabled: boolean;
  up: boolean;
  version: string | null;
  uptime_s: number | null;
  /** The node's check of its scope's current target: null = not checked yet (or no key yet). */
  target_ok: boolean | null;
  apply_error: string | null;
  reported_at: string | null;
  /** The XHTTP inbound's port while the node runs it (null: none). */
  xhttp_port: number | null;
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
  /** host:port of the target this node's links use now. */
  target: string | null;
  /** The owner's newer target, while this slot waits for its current configs to end. */
  target_next: string | null;
  /** The node's own DNS name (slot<N>.<domain> or t<8 hex>.<domain>) if Cloudflare has it. */
  dns_name: string | null;
  /** That name points at exactly this node (a slot's name moves to the newest server). */
  dns_own: boolean;
  /** The scope's WebSocket path (ws_tls links). */
  ws_path: string | null;
  /** The node's agent can serve ws_tls (0.3.0 or newer). */
  agent_ws_tls: boolean;
  /** The node's agent can serve XHTTP (0.4.0 or newer). */
  agent_xhttp: boolean;
  active_configs: number;
}

export interface RealityCertificate {
  domain: string;
  names: string[];
  /** Valid now (and not about to end): nodes may serve it. */
  valid: boolean;
  not_after: string | null;
  issued_at: string | null;
  /** Short code of the last failed order, while it has not succeeded since. */
  last_error: string | null;
  retry_at: string | null;
}

export interface RealityConfig {
  id: number;
  key: string;
  name: string;
  slot: number | null;
  node_name: string;
  location: string;
  host: string;
  transport: Transport;
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
  /** The same config through VLESS + XHTTP + REALITY (null: ws_tls, or XHTTP off). */
  xhttp: RealityXhttpLink | null;
}

export interface RealityXhttpLink {
  key: string;
  /** Handed to posts (its node reported the XHTTP inbound up). */
  listed: boolean;
  /** Active configs only. */
  url: string | null;
}

export interface RealitySummary {
  settings: RealitySettingsView;
  defaults: RealitySettingsView;
  /** The wildcard certificate of the active domain (null: no active domain). */
  certificate: RealityCertificate | null;
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

export const TRANSPORT: Record<Transport, { label: string; short: string; hint: string }> = {
  ws_tls: {
    label: "WebSocket + TLS با دامنه‌ی خود node",
    short: "WS + TLS",
    hint: "مثل کانفیگ سالم شما: هر node نام دامنه‌ی خودش را دارد (slot۱.دامنه یا t….دامنه) با گواهی واقعی Let's Encrypt، پس نامی که فیلتر میبیند با IP یکی است.",
  },
  reality: {
    label: "Reality با سایت هدف خارجی",
    short: "Reality",
    hint: "VLESS + REALITY روی TCP با نام سایت هدف (مثل dl.google.com). با XHTTP روشن، هر کانفیگ لینک دومی هم دارد: همان کانفیگ با XHTTP روی پورت ۲۰۸۳.",
  },
};

/** The Xray state of a node in one Persian phrase and a tone. With ws_tls, ``target_ok`` is the
 * node's own TLS check of its inbound (its name and certificate), not a foreign target. */
export function xrayState(x: RealityXray | null, transport: Transport = "reality"): { label: string; tone: Tone } {
  if (x === null) return { label: "عامل قدیمی؛ Xray ندارد", tone: "neutral" };
  if (!x.enabled) return { label: "Xray روی این node نصب نیست", tone: "neutral" };
  if (x.apply_error) return { label: APPLY_ERRORS[x.apply_error] ?? "خطا در اعمال تنظیمات Xray", tone: "danger" };
  const ws = transport === "ws_tls";
  if (x.target_ok === false) {
    return { label: ws ? "آزمون TLS خود node با گواهی‌اش رد شد" : "سایت هدف از این node جواب درست نداد", tone: "danger" };
  }
  if (!x.up) return { label: "Xray هنوز بالا نیامده", tone: "warning" };
  if (x.target_ok === null) {
    return { label: ws ? "Xray فعال است؛ TLS خود node هنوز آزموده نشده" : "Xray فعال است؛ سایت هدف هنوز آزموده نشده", tone: "info" };
  }
  return { label: "Xray فعال است", tone: "success" };
}

/** The XHTTP inbound of a node in one Persian phrase and a tone (null: nothing to say, e.g. ws_tls). */
export function xhttpState(n: Pick<RealityNode, "agent_xhttp" | "xray">, s: Pick<RealitySettings, "transport" | "xhttp_enabled">): { label: string; tone: Tone } | null {
  if (s.transport !== "reality") return null;
  if (!s.xhttp_enabled) return { label: "XHTTP خاموش است", tone: "neutral" };
  if (!n.agent_xhttp) return { label: "عامل این node قدیمی است (XHTTP از نسخه‌ی ۰٫۴٫۰)", tone: "neutral" };
  if (n.xray?.up && n.xray.xhttp_port !== null) return { label: `XHTTP روی پورت ${toFaDigits(String(n.xray.xhttp_port))} فعال است`, tone: "success" };
  return { label: "XHTTP هنوز روی node بالا نیامده", tone: "info" };
}

/** Why a node gets no ws_tls inbound yet (null: it has everything). */
export function wsBlocker(n: Pick<RealityNode, "dns_name" | "dns_own" | "agent_ws_tls">, cert: RealityCertificate | null): string | null {
  if (!n.agent_ws_tls) return "عامل این node قدیمی است (WS + TLS از نسخه‌ی ۰٫۳٫۰)";
  if (n.dns_name === null) return "هنوز نام دامنه ندارد";
  if (cert === null || !cert.valid) return "گواهی دامنه هنوز آماده نیست";
  if (!n.dns_own) return "نامش به سرور تازه‌ی همین جایگاه رفته است";
  return null;
}

/** The certificate in one Persian phrase and a tone. */
export function certificateState(cert: RealityCertificate | null): { label: string; tone: Tone } {
  if (cert === null) return { label: "دامنه‌ی فعالی نیست؛ گواهی گرفته نمیشود", tone: "neutral" };
  if (cert.valid) return { label: "گواهی معتبر است", tone: "success" };
  if (cert.last_error) return { label: "گرفتن گواهی نشد؛ پنل دوباره تلاش میکند", tone: "danger" };
  return { label: "گواهی در راه است", tone: "info" };
}

/** What happens after the owner saves the target: the words for the toast. */
export function savedMessage(before: RealitySettings, after: RealitySettings): string {
  if (before.transport !== after.transport) {
    return after.transport === "ws_tls"
      ? "nodeها به WebSocket + TLS میروند؛ کانفیگ‌های Reality کنار میروند (🚫 روی پست) و کانفیگ تازه با نام دامنه‌ی هر node ساخته میشود."
      : "nodeها به Reality برمیگردند؛ کانفیگ‌های WebSocket + TLS کنار میروند (🚫 روی پست) و کانفیگ تازه‌ی Reality ساخته میشود.";
  }
  if (before.xhttp_enabled !== after.xhttp_enabled && after.transport === "reality") {
    return after.xhttp_enabled
      ? "nodeها XHTTP را روی پورت ۲۰۸۳ بالا میآورند؛ هر کانفیگ پس از تأیید node لینک XHTTP هم میگیرد."
      : "XHTTP از nodeها برداشته میشود؛ لینک‌های XHTTP پست‌ها بی‌لینک (⏸) میمانند و کانفیگ‌ها با TCP کار میکنند.";
  }
  if (before.target_host !== after.target_host || before.target_port !== after.target_port) {
    return "هر جایگاه پس از پایان کانفیگ‌های فعلی‌اش به سایت هدف تازه میرود (اگر سایت قبلی از node جواب نمیداد، بی‌درنگ). node سایت تازه را میآزماید و بعد کانفیگ تازه میگیرد.";
  }
  return "کانفیگ‌های تازه با این عددها ساخته میشوند.";
}

/** Hint under the target field: what changing it really does. */
export const TARGET_HINT =
  "یک سایت خارجی بزرگ با TLS 1.3 و HTTP/2. با تغییر آن، هر جایگاه پس از پایان کانفیگ‌های فعلی‌اش به سایت تازه میرود؛ اگر سایت قبلی از node جواب نمیداد، بی‌درنگ. لینک‌های پست‌شده تا پایانشان کار میکنند.";

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
  transport: Transport;
  xhttp_enabled: boolean;
  renew: boolean;
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
    transport: s.transport,
    xhttp_enabled: s.xhttp_enabled,
    renew: s.renew,
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
      transport: d.transport,
      xhttp_enabled: d.xhttp_enabled,
      renew: d.renew,
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
    a.transport === b.transport &&
    a.xhttp_enabled === b.xhttp_enabled &&
    a.renew === b.renew &&
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
