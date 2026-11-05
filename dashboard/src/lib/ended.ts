// The owner's request (2026-11-05): the MTProto, Reality and Nodes pages show only what works or is
// on its way in the main list; whatever was deleted, ended, burned or expired goes into a collapsed
// «پایان‌یافته‌ها» section with its count. Pure helpers, unit-tested; the server lists the same items
// as before (old clients keep working), the split happens here.

/** Items in their original order, split into the main list and the ended ones. */
export function splitEnded<T>(items: readonly T[], isEnded: (item: T) => boolean): { current: T[]; ended: T[] } {
  const current: T[] = [];
  const ended: T[] = [];
  for (const item of items) (isEnded(item) ? ended : current).push(item);
  return { current, ended };
}

/** Node states that are on their way out or gone: burned (filtered from Iran), draining, retiring
 * (being deleted) and deleted. Requested, creating, booting and registering are on their way in;
 * ready, unhealthy and repairing are in service. */
export const ENDED_NODE_STATES: ReadonlySet<string> = new Set(["burned", "draining", "retiring", "deleted"]);

export function isEndedNode(n: { state: string }): boolean {
  return ENDED_NODE_STATES.has(n.state);
}

/** An MTProto proxy whose node left service (expired) or was filtered (burned). Pending and
 * active stay in the main list; an unknown status from a newer server too. */
export function isEndedProxy(p: { status: string }): boolean {
  return p.status === "expired" || p.status === "burned";
}

/** A Reality config that ended: its time, its volume, or its server or domain (burned). */
export function isEndedConfig(c: { status: string }): boolean {
  return c.status === "expired" || c.status === "full" || c.status === "burned";
}
