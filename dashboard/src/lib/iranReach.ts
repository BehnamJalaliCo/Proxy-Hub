// What Iran really reaches (P15): pure, unit-tested helpers for the location x network table on the
// Nodes page and the candidate sites on the Settings page. A cell is a pass/fail fact about OUR
// servers or a public site, measured by a real TLS handshake from an operator's network; it wears a
// status tone and always carries a word, never colour alone. Networks are operators, never people.

import { formatNumber, toFaDigits } from "./format";
import { operatorName, type Tone } from "./labels";
import type { LocationClass, LocationReach, ProbeNetwork, TargetCandidate, TargetNetwork } from "./types";

export const LOCATION_CLASS: Record<LocationClass, { label: string; tone: Tone; hint: string }> = {
  good: {
    label: "باز",
    tone: "success",
    hint: "از همراه اول و رایتل باز بوده است؛ سرور تازه اول اینجا ساخته میشود.",
  },
  mixed: {
    label: "نیمه‌باز",
    tone: "warning",
    hint: "از یکی از دو اپراتور باز و از دیگری بسته (یا هنوز سنجیده نشده) بوده است.",
  },
  unknown: {
    label: "سنجیده نشده",
    tone: "neutral",
    hint: "در ۷ روز گذشته node ای از ما اینجا سنجیده نشده است.",
  },
  bad: {
    label: "بسته",
    tone: "danger",
    hint: "از همراه اول و رایتل بسته بوده است (دست‌دادن TLS ناموفق)؛ سرور تازه اینجا ساخته نمیشود.",
  },
};

export function locationClassInfo(c: string): { label: string; tone: Tone; hint: string } {
  return (LOCATION_CLASS as Record<string, { label: string; tone: Tone; hint: string } | undefined>)[c] ?? LOCATION_CLASS.unknown;
}

/** A network key ("asn:197207") -> its Persian operator name when well known. */
export function networkLabel(key: string, fallback = ""): string {
  const m = /^asn:(\d+)$/.exec(key);
  const asn = m ? Number(m[1]) : null;
  return operatorName(asn) ?? (fallback || key);
}

export interface ReachCell {
  state: "open" | "closed" | null;
  tone: Tone;
  text: string;
  title: string;
}

const NO_DATA: ReachCell = { state: null, tone: "neutral", text: "—", title: "سنجیده نشده" };

/** The location table: one column per key network (MCI, Rightel), then "others" (open if any other
 * Iranian network reached one of our nodes there). */
export function locationRow(loc: LocationReach, keyNetworks: string[]): { cells: ReachCell[]; others: ReachCell } {
  const cell = (state: "open" | "closed", open: number, closed: number): ReachCell => ({
    state,
    tone: state === "open" ? "success" : "danger",
    text: state === "open" ? "باز" : "بسته",
    title: `${formatNumber(open)} node باز، ${formatNumber(closed)} node بسته`,
  });
  const cells = keyNetworks.map((k) => {
    const n = loc.networks.find((x) => x.key === k);
    return n ? cell(n.state, n.nodes_open, n.nodes_closed) : NO_DATA;
  });
  const rest = loc.networks.filter((n) => !keyNetworks.includes(n.key));
  if (rest.length === 0) return { cells, others: NO_DATA };
  const open = rest.filter((n) => n.state === "open");
  const others = cell(open.length > 0 ? "open" : "closed", open.length, rest.length - open.length);
  others.title = rest.map((n) => `${n.network}: ${n.state === "open" ? "باز" : "بسته"}`).join(" · ");
  return { cells, others };
}

/** Allowed locations that are closed on every key network, for the warning above the table. */
export function closedAllowed(locations: LocationReach[]): string[] {
  return locations.filter((l) => l.allowed && l.class === "bad").map((l) => l.location);
}

// --- the TLS handshakes of one node ----------------------------------------------------------------

export const FAILURE_FA: Record<string, string> = {
  tls_timeout: "دست‌دادن TLS تمام نشد",
  tcp_timeout: "اتصال برقرار نشد",
  reset: "اتصال قطع شد",
  other: "خطا",
};

/** A short Persian reason for a failed handshake. */
export function failureText(code: string | null | undefined): string {
  if (!code) return "";
  return FAILURE_FA[code] ?? FAILURE_FA["other"] ?? code;
}

/** The ports a node's handshakes went to, as "۴۴۳ و ۸۴۴۳". */
export function portsText(networks: ProbeNetwork[]): string {
  const ports = [...new Set(networks.map((n) => n.port).filter((p): p is number => typeof p === "number"))].sort((a, b) => a - b);
  return ports.map((p) => toFaDigits(String(p))).join(" و ");
}

// --- candidate sites ---------------------------------------------------------------------------------

export function candidateState(c: TargetCandidate): { label: string; tone: Tone } {
  if (c.blocked_on.length > 0) return { label: `بسته از ${c.blocked_on.map((k) => networkLabel(k)).join(" و ")}`, tone: "danger" };
  if (c.measured_at === null) return { label: "هنوز سنجیده نشده", tone: "neutral" };
  if (c.open) return { label: "باز از ایران", tone: "success" };
  if (c.networks.some((n) => n.key_network && n.state === "down")) return { label: "یک بار بسته دیده شد", tone: "warning" };
  return { label: "بی نتیجه‌ی معتبر", tone: "neutral" };
}

export function targetCell(n: TargetNetwork | undefined): { tone: Tone; text: string } {
  if (!n || n.state === "nodata") return { tone: "neutral", text: "—" };
  if (n.state === "down") return { tone: "danger", text: "بسته" };
  return { tone: "success", text: n.ms !== null ? `${formatNumber(Math.round(n.ms))} ms` : "باز" };
}

export function nodeCheckInfo(c: TargetCandidate): { label: string; tone: Tone } {
  if (c.node_check === "passed") return { label: "nodeها قبول کردند", tone: "success" };
  if (c.node_check === "failed") return { label: "nodeها رد کردند", tone: "danger" };
  return { label: "—", tone: "neutral" };
}

export const CURRENT_FOR: Record<string, string> = {
  reality: "هدف Reality",
  mtproto: "دامنه‌ی MTProto",
};

const HOST = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/;

/** " WWW.Apple.com. " -> "www.apple.com" (as the server stores it). */
export function normalizeHost(raw: string): string {
  return raw.trim().toLowerCase().replace(/\.+$/, "");
}

/** Xray 26.10.31's own warning for a REALITY server name (infra/conf/transport_security.go), as the
 * panel refuses it (panel/proxyhub/targets/risky.py): apple, icloud or microsoft anywhere in the
 * name, or a name ending in .ru, .ir or .cn. */
export function riskySni(host: string): boolean {
  const sn = normalizeHost(host);
  return [".ru", ".ir", ".cn"].some((s) => sn.endsWith(s)) || ["apple", "icloud", "microsoft"].some((w) => sn.includes(w));
}

/** The candidate list's problem (Persian), or null: 1 to 12 DNS names, not IPs, none Xray warns about. */
export function candidatesProblem(hosts: string[]): string | null {
  if (hosts.length === 0) return "دست‌کم یک سایت نامزد لازم است.";
  if (hosts.length > 12) return "حداکثر ۱۲ سایت نامزد.";
  const bad = hosts.find((h) => !HOST.test(h) || /^[\d.]+$/.test(h));
  if (bad !== undefined) return `«${bad}» نام دامنه نیست (مثلاً github.com، نه IP).`;
  const risky = hosts.find(riskySni);
  return risky === undefined
    ? null
    : `«${risky}» سایت هدف نمیشود: خود Xray هشدار میدهد که نامی با apple، icloud یا microsoft (یا پایان ‎.ru، ‎.ir و ‎.cn) احتمال بسته شدن IP سرور را بالا میبرد.`;
}
