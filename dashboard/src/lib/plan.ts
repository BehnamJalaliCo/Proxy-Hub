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
