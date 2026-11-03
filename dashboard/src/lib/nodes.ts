// Node list helpers (pure, unit-tested): order, the one test node, agent health and readable
// node events. Node addresses are our own servers' (rule 7 is about end users: none here).

import { formatJalali, formatNumber } from "./format";
import { nodeStateInfo, reasonLabel, transitionReasonLabel, type Tone } from "./labels";
import type { NodeAgent, NodeEventItem, NodeInfo } from "./types";

/** Milliseconds of an ISO time; 0 if unreadable (sorts last when newest-first). */
function time(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
}

export function isLiveNode(n: Pick<NodeInfo, "state">): boolean {
  return n.state !== "deleted";
}

/** The owner's test node that still exists (the server allows one at a time). */
export function liveTestNode(nodes: NodeInfo[]): NodeInfo | undefined {
  return nodes.find((n) => n.is_test && isLiveNode(n));
}

/** The server decides; the button is only offered where a retirement can make sense. */
export function canRetire(n: Pick<NodeInfo, "state">): boolean {
  return n.state !== "retiring" && n.state !== "deleted";
}

/** Live nodes first (test node, then by slot), deleted ones last; newest first within a group. */
export function sortNodes(nodes: NodeInfo[]): NodeInfo[] {
  const rank = (n: NodeInfo) => (isLiveNode(n) ? (n.is_test ? 0 : 1) : 2);
  return [...nodes].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      (a.slot ?? Number.MAX_SAFE_INTEGER) - (b.slot ?? Number.MAX_SAFE_INTEGER) ||
      time(b.created_at) - time(a.created_at),
  );
}

export function countByState(nodes: NodeInfo[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const n of nodes) m.set(n.state, (m.get(n.state) ?? 0) + 1);
  return m;
}

/** How fresh the agent's last report is. The agent reports about once a minute. */
export function agentHealth(agent: NodeAgent, now: Date = new Date()): { tone: Tone; label: string } {
  if (!agent.registered) return { tone: "neutral", label: "ثبت نشده" };
  if (!agent.last_seen_at) return { tone: "warning", label: "هنوز گزارشی نفرستاده" };
  const age = (now.getTime() - new Date(agent.last_seen_at).getTime()) / 1000;
  if (!Number.isFinite(age)) return { tone: "warning", label: "زمان گزارش نامعلوم" };
  if (age <= 180) return { tone: "success", label: "زنده" };
  if (age <= 900) return { tone: "warning", label: "دیر کرده" };
  return { tone: "danger", label: "قطع" };
}

// --- node events --------------------------------------------------------------------------------

const DETAIL_KEY: Record<string, string> = {
  hetzner_id: "شناسه‌ی هتزنر",
  ipv4: "IP سرور",
  location: "location",
  server_type: "نوع سرور",
  account_id: "حساب",
  image: "image",
  code: "کد",
  transient: "گذرا",
  found: "تعداد پیداشده",
  serial: "سریال گواهی",
  not_after: "اعتبار گواهی تا",
  by: "انجام‌دهنده",
  dry_run: "حالت خشک",
  allowed: "مجاز",
  error: "خطا",
};

/** Keys shown elsewhere in the row (or never). */
const HIDDEN = new Set(["from", "to", "reason", "reasons"]);

function valueText(key: string, v: unknown): string | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "boolean") return v ? "بله" : "خیر";
  if (typeof v === "number") return key === "hetzner_id" ? String(v) : formatNumber(v);
  if (typeof v === "string") {
    if (key === "not_after" && !Number.isNaN(Date.parse(v))) return formatJalali(v, "date");
    return v;
  }
  try {
    return JSON.stringify(v);
  } catch {
    return null;
  }
}

export interface EventView {
  /** "از «آماده» به «در حال بازنشستگی»" for state changes; null otherwise. */
  change: { from: string | null; to: string | null } | null;
  /** Why (a transition reason) and/or the refusal reasons, in Persian. */
  reasons: string[];
  /** Other detail fields as label/value pairs (Latin values are shown left-to-right). */
  fields: { key: string; label: string; value: string }[];
}

export function eventView(e: NodeEventItem): EventView {
  const d = e.detail;
  const str = (k: string) => (typeof d[k] === "string" ? d[k] : null);
  const change =
    e.kind === "state"
      ? {
          from: str("from") ? nodeStateInfo(str("from") ?? "").label : null,
          to: str("to") ? nodeStateInfo(str("to") ?? "").label : null,
        }
      : null;
  const reasons: string[] = [];
  const reason = str("reason");
  if (reason) reasons.push(transitionReasonLabel(reason));
  const list = d["reasons"];
  if (Array.isArray(list)) for (const r of list) if (typeof r === "string") reasons.push(reasonLabel(r));
  const fields: EventView["fields"] = [];
  for (const [k, v] of Object.entries(d)) {
    if (HIDDEN.has(k)) continue;
    const value = valueText(k, v);
    if (value !== null) fields.push({ key: k, label: DETAIL_KEY[k] ?? k, value });
  }
  return { change, reasons, fields };
}

/** Newest first (the API order is not part of the contract). */
export function sortEvents(events: NodeEventItem[]): NodeEventItem[] {
  return [...events].sort((a, b) => time(b.at) - time(a.at));
}
