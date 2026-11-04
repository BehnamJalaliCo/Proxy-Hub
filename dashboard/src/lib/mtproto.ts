// MTProto proxies on the nodes (GET /api/mtproto, PUT /api/mtproto/settings): types, the two API
// calls, the query hook, Persian words, and pure, unit-tested helpers for the settings form that
// mirror the server's rules (panel/proxyhub/mtproto/settings.py). The server is always the authority.
// A proxy's link holds its secret: it is shown and copied only for the owner to publish, never logged.

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing, request } from "./api";
import type { Tone } from "./labels";
import { asciiDigits } from "./validation";

export type MtprotoMode = "tls" | "secure" | "classic";
export type MtprotoStatus = "pending" | "active" | "expired" | "burned";

export interface MtprotoSettings {
  port: number;
  tls_domain: string;
  mode_tls: boolean;
  mode_secure: boolean;
  mode_classic: boolean;
  link_mode: MtprotoMode;
}

export type MtprotoSettingsInput = Omit<MtprotoSettings, "link_mode">;

export interface MtprotoProxy {
  key: string;
  node_id: string;
  node_name: string;
  node_state: string;
  is_test: boolean;
  slot: number | null;
  label: string;
  host: string | null;
  host_kind: "domain" | "ip" | null;
  port: number;
  mode: MtprotoMode;
  tls_domain: string;
  link: string | null;
  status: MtprotoStatus;
  connections: number | null;
  total_octets: number | null;
  stats_at: string | null;
  live_at: string | null;
  sponsor_tag_set: boolean;
}

export interface MtprotoState {
  settings: MtprotoSettings | null;
  settings_corrupt: boolean;
  defaults: MtprotoSettings;
  active_domain: string | null;
  proxies: MtprotoProxy[];
}

// --- API -------------------------------------------------------------------------------------------

export const getMtproto = (signal?: AbortSignal) =>
  request<MtprotoState>("GET", "/api/mtproto", undefined, signal ? { signal } : {});

export const putMtprotoSettings = (body: MtprotoSettingsInput) =>
  request<{ settings: MtprotoSettings }>("PUT", "/api/mtproto/settings", body);

export const mtprotoKey = ["mtproto"] as const;

export const useMtproto = () =>
  useQuery({
    queryKey: mtprotoKey,
    queryFn: ({ signal }) => getMtproto(signal),
    // Counters come from the node agents once a minute; stop polling while the route is missing.
    refetchInterval: (query) => (isEndpointMissing(query.state.error) ? false : 30_000),
  });

// --- words -------------------------------------------------------------------------------------------

export const MTPROTO_STATUS: Record<MtprotoStatus, { label: string; tone: Tone; hint: string }> = {
  pending: {
    label: "در حال راه‌اندازی",
    tone: "info",
    hint: "پنل تنظیم را ساخته است؛ وقتی عامل node آن را اجرا کرد و گزارش داد، لینک فعال و قابل انتشار میشود.",
  },
  active: { label: "فعال", tone: "success", hint: "روی node اجرا میشود و لینکش قابل انتشار است." },
  expired: {
    label: "تمام‌شده",
    tone: "neutral",
    hint: "node از کار خارج شده یا در حال جایگزینی است؛ این لینک دیگر منتشر نمیشود.",
  },
  burned: { label: "فیلترشده", tone: "danger", hint: "سنجش از ایران این node را فیلترشده نشان داد." },
};

export function mtprotoStatusInfo(status: string): { label: string; tone: Tone; hint: string } {
  return (MTPROTO_STATUS as Record<string, { label: string; tone: Tone; hint: string } | undefined>)[status] ?? {
    label: status,
    tone: "neutral",
    hint: "",
  };
}

export const MODE_LABEL: Record<MtprotoMode, { label: string; short: string; hint: string }> = {
  tls: {
    label: "Fake-TLS (ee)",
    short: "TLS",
    hint: "ترافیک مثل اتصال HTTPS به دامنه‌ی TLS دیده میشود. اپ تلگرام باید به‌روز باشد.",
  },
  secure: { label: "Secure (dd)", short: "dd", hint: "MTProto مبهم‌شده با padding، بی شکل TLS." },
  classic: { label: "Classic", short: "ساده", hint: "secret ساده‌ی ۱۶ بایتی (مثل رقیب روی پورت ۸۸۸۰)." },
};

// --- settings form -----------------------------------------------------------------------------------

/** Ports a proxy may never use on the nodes (FORBIDDEN_PORTS in panel/proxyhub/mtproto/settings.py). */
export const FORBIDDEN_PORTS: ReadonlyMap<number, string> = new Map([
  [22, "پورت ۲۲ برای SSH است و فایروال node آن را میبندد."],
  [443, "پورت ۴۴۳ مال VLESS Reality روی همه‌ی nodeهاست."],
  [9090, "پورت ۹۰۹۰ مال آمار داخلی Telemt است."],
  [9091, "پورت ۹۰۹۱ مال API داخلی Telemt است."],
]);

export interface MtprotoDraft {
  port: string;
  tls_domain: string;
  mode_tls: boolean;
  mode_secure: boolean;
  mode_classic: boolean;
}

export type MtprotoDraftErrors = Partial<Record<"port" | "tls_domain" | "modes", string>>;

export function draftFromMtproto(s: MtprotoSettings): MtprotoDraft {
  return {
    port: String(s.port),
    tls_domain: s.tls_domain,
    mode_tls: s.mode_tls,
    mode_secure: s.mode_secure,
    mode_classic: s.mode_classic,
  };
}

/** "۸۴۴۳" -> 8443; a Persian hint when it is not a usable proxy port. */
export function parsePort(raw: string): { ok: true; value: number } | { ok: false; error: string } {
  const s = asciiDigits(raw.trim());
  if (s === "") return { ok: false, error: "پورت را وارد کنید (پیش‌فرض ۸۴۴۳)." };
  if (!/^\d{1,5}$/.test(s)) return { ok: false, error: "پورت فقط عدد است، از ۱ تا ۶۵۵۳۵." };
  const n = Number(s);
  if (n < 1 || n > 65535) return { ok: false, error: "پورت باید از ۱ تا ۶۵۵۳۵ باشد." };
  const reserved = FORBIDDEN_PORTS.get(n);
  if (reserved) return { ok: false, error: reserved };
  return { ok: true, value: n };
}

const DOMAIN = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/;
const IPV4ISH = /^\d{1,3}(?:\.\d{1,3}){3}$/;

/** " WWW.Apple.COM. " -> "www.apple.com" (as the server stores it). */
export function normalizeDomain(raw: string): string {
  return raw.trim().toLowerCase().replace(/\.+$/, "");
}

export function domainProblem(raw: string): string | null {
  const d = normalizeDomain(raw);
  if (d === "") return "دامنه‌ی TLS را وارد کنید (پیش‌فرض www.microsoft.com).";
  if (IPV4ISH.test(d) || d.includes(":")) return "نام دامنه لازم است، نه IP.";
  if (!DOMAIN.test(d)) return "این شکل نام دامنه نیست (مثلاً www.microsoft.com، بی فاصله و بی /).";
  return null;
}

/** The first mode that is on, as the server picks the one link of each proxy. */
export function linkModeOf(d: Pick<MtprotoDraft, "mode_tls" | "mode_secure" | "mode_classic">): MtprotoMode | null {
  if (d.mode_tls) return "tls";
  if (d.mode_secure) return "secure";
  if (d.mode_classic) return "classic";
  return null;
}

export function mtprotoFromDraft(d: MtprotoDraft): { value: MtprotoSettingsInput | null; errors: MtprotoDraftErrors } {
  const errors: MtprotoDraftErrors = {};
  const port = parsePort(d.port);
  if (!port.ok) errors.port = port.error;
  const domainError = domainProblem(d.tls_domain);
  if (domainError) errors.tls_domain = domainError;
  if (linkModeOf(d) === null) errors.modes = "دست‌کم یک حالت باید روشن باشد.";
  if (!port.ok || Object.keys(errors).length > 0) return { value: null, errors };
  return {
    value: {
      port: port.value,
      tls_domain: normalizeDomain(d.tls_domain),
      mode_tls: d.mode_tls,
      mode_secure: d.mode_secure,
      mode_classic: d.mode_classic,
    },
    errors,
  };
}

/** What a save changes in every published link (port, domain of ee links, link mode), if anything. */
export function linkChanges(before: MtprotoSettings, after: MtprotoSettingsInput): string[] {
  const out: string[] = [];
  if (before.port !== after.port) out.push("پورت");
  const mode = linkModeOf(after);
  if (mode !== before.link_mode) out.push("نوع لینک");
  if (mode === "tls" && before.link_mode === "tls" && before.tls_domain !== after.tls_domain) out.push("دامنه‌ی TLS");
  return out;
}

/** 422 invalid_settings {"fields": [...]} -> which form fields the server rejected. */
export function serverErrors(fields: string[]): MtprotoDraftErrors {
  const out: MtprotoDraftErrors = {};
  for (const f of fields) {
    if (f === "port") out.port = "سرور این پورت را نپذیرفت.";
    else if (f === "tls_domain") out.tls_domain = "سرور این دامنه را نپذیرفت.";
    else if (f === "modes" || f.startsWith("mode_")) out.modes = "سرور این ترکیب حالت‌ها را نپذیرفت.";
  }
  return out;
}

// --- list ---------------------------------------------------------------------------------------------

export interface MtprotoSummary {
  active: number;
  pending: number;
  connections: number | null;
  sponsored: number;
}

export function summarize(proxies: MtprotoProxy[]): MtprotoSummary {
  const active = proxies.filter((p) => p.status === "active");
  const counted = active.filter((p) => p.connections !== null);
  return {
    active: active.length,
    pending: proxies.filter((p) => p.status === "pending").length,
    connections: counted.length ? counted.reduce((s, p) => s + (p.connections ?? 0), 0) : null,
    sponsored: active.filter((p) => p.sponsor_tag_set).length,
  };
}

/** "slot3.example.com:8443" for display (the host is LTR text). */
export function endpointText(p: Pick<MtprotoProxy, "host" | "port">): string {
  return p.host === null ? "—" : `${p.host}:${String(p.port)}`;
}

/** Bytes for people: 1536 -> "1.5 KB" (Latin units; digits are localised by the caller). */
export function formatBytes(n: number | null): string {
  if (n === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 || i === 0 ? String(Math.round(v)) : v.toFixed(1)} ${units[i] ?? "B"}`;
}
