// Grouping of audit events for charts and filters (pure functions, unit-tested).

import { formatJalali, tehranDayKey } from "./format";
import type { AuditEvent } from "./types";

export type Verdict = "allowed" | "blocked" | "info";

/** allowed=true: the gate let it through; false: a gate blocked it; null: not a gated action. */
export function verdictOf(e: Pick<AuditEvent, "allowed">): Verdict {
  if (e.allowed === true) return "allowed";
  if (e.allowed === false) return "blocked";
  return "info";
}

export interface DayBucket {
  key: string;
  label: string;
  allowed: number;
  blocked: number;
  info: number;
}

/** The last `days` Tehran calendar days (oldest first), each with its event counts. */
export function bucketByDay(events: AuditEvent[], days: number, now: Date = new Date()): DayBucket[] {
  const buckets: DayBucket[] = [];
  const index = new Map<string, DayBucket>();
  for (let i = days - 1; i >= 0; i--) {
    // Iran has no daylight saving time since 2022, so whole days are 24h apart.
    const d = new Date(now.getTime() - i * 86_400_000);
    const key = tehranDayKey(d);
    if (index.has(key)) continue;
    const b: DayBucket = { key, label: formatJalali(d, "day"), allowed: 0, blocked: 0, info: 0 };
    buckets.push(b);
    index.set(key, b);
  }
  for (const e of events) {
    const b = index.get(tehranDayKey(e.at));
    if (b) b[verdictOf(e)] += 1;
  }
  return buckets;
}

/** How many of the last `days` Tehran days the loaded events fully cover. A full page (`limit`
 * events, the newest ones) is cut somewhere in its oldest day, and the days before that have
 * nothing loaded: drawing them would show busy days as quiet. At least today is drawn. */
export function coveredDays(events: Pick<AuditEvent, "at">[], limit: number, days: number, now: Date = new Date()): number {
  if (events.length < limit) return days;
  let oldest = Number.POSITIVE_INFINITY;
  for (const e of events) {
    const t = Date.parse(e.at);
    if (!Number.isNaN(t) && t < oldest) oldest = t;
  }
  if (!Number.isFinite(oldest)) return days;
  const cut = tehranDayKey(new Date(oldest));
  for (let i = 0; i < days; i++) {
    if (tehranDayKey(new Date(now.getTime() - i * 86_400_000)) === cut) return Math.max(1, i);
  }
  return days;
}

export interface EventFilter {
  text: string;
  verdict: "all" | Verdict;
  mode: "all" | "dry" | "real";
  actor: string;
}

export const EMPTY_FILTER: EventFilter = { text: "", verdict: "all", mode: "all", actor: "" };

export function filterEvents(events: AuditEvent[], f: EventFilter, labelOf: (e: AuditEvent) => string): AuditEvent[] {
  const q = f.text.trim().toLowerCase();
  return events.filter((e) => {
    if (f.verdict !== "all" && verdictOf(e) !== f.verdict) return false;
    if (f.mode === "dry" && !e.dry_run) return false;
    if (f.mode === "real" && e.dry_run) return false;
    if (f.actor && e.actor !== f.actor) return false;
    if (q) {
      const hay = [e.action, e.actor, e.target ?? "", labelOf(e), ...(e.reasons ?? []), resultText(e.result)]
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

/** The `result` column as one line of text, whatever JSON type it holds. */
export function resultText(result: unknown): string {
  if (result === null || result === undefined) return "";
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") return String(result);
  try {
    return JSON.stringify(result);
  } catch {
    return "";
  }
}
