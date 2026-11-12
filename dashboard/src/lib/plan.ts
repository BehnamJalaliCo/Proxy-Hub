import type { PlanAction } from "./types";

export interface PlanGroup {
  action: PlanAction["action"];
  allowed: boolean;
  reasons: string[];
  targets: string[];
}

/** Actions with the same kind, verdict and reasons are shown once with all their targets
 * (nine blocked "create" slots read as one line, not nine identical ones). Order is kept. */
export function groupPlan(actions: PlanAction[]): PlanGroup[] {
  const groups = new Map<string, PlanGroup>();
  for (const a of actions) {
    const reasons = [...a.reasons].sort();
    const key = `${a.action}|${String(a.allowed)}|${reasons.join(",")}`;
    const g = groups.get(key);
    if (g) g.targets.push(a.target);
    else groups.set(key, { action: a.action, allowed: a.allowed, reasons: a.reasons, targets: [a.target] });
  }
  return [...groups.values()];
}

/** The planner writes one action per slot on every run; "none" (allowed=false, e.g. slot_healthy)
 * is a slot with nothing to do, not a refusal. Only create/delete are work a gate can refuse. */
export function planCounts(actions: PlanAction[]): { work: number; blocked: number; idle: number } {
  let work = 0;
  let blocked = 0;
  let idle = 0;
  for (const a of actions) {
    if (a.action === "none") idle += 1;
    else {
      work += 1;
      if (!a.allowed) blocked += 1;
    }
  }
  return { work, blocked, idle };
}

const HEALTHY = "slot_healthy";

/** Nothing to do and nothing to tell: no work and every idle slot is healthy (or no slot at all). */
export function planAllHealthy(actions: PlanAction[]): boolean {
  return actions.every((a) => a.action === "none" && a.reasons.every((r) => r === HEALTHY));
}

/** The rows to list: the healthy idle slots are left out (the header says it), every other idle
 * reason (starting, settling, backoff, ...) stays because it tells the owner something. */
export function planRows(actions: PlanAction[]): PlanGroup[] {
  return groupPlan(actions.filter((a) => !(a.action === "none" && a.reasons.every((r) => r === HEALTHY))));
}
