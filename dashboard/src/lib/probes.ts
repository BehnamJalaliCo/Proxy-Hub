// Reachability from Iran as a node x network grid (pure, unit-tested). A cell means "did our node
// answer from this operator's network": a pass/fail fact, so it wears the status tones and always
// carries its numbers and an icon, never colour alone. Networks are operators/ASNs, never people.

import { operatorName, operatorRank, type Tone } from "./labels";
import type { ProbeNetwork, ProbeTarget, ProbeVerdict } from "./types";

export interface HeatColumn {
  key: string;
  /** Persian operator name when the ASN is a well-known one, else the network's own name. */
  label: string;
  network: string;
  asn: number | null;
}

export interface HeatCell {
  ok: number;
  fail: number;
  at: string;
  tone: Tone;
}

export interface HeatRow {
  target: ProbeTarget;
  /** One per column, null where this node was not measured from that network. */
  cells: (HeatCell | null)[];
}

export function networkKey(n: Pick<ProbeNetwork, "network" | "asn">): string {
  return n.asn !== null ? `AS${String(n.asn)}` : `net:${n.network}`;
}

/** ok only -> success; failures only -> danger; both -> warning; nothing measured -> neutral. */
export function cellTone(ok: number, fail: number): Tone {
  if (ok + fail <= 0) return "neutral";
  if (fail === 0) return "success";
  if (ok === 0) return "danger";
  return "warning";
}

/** Columns: well-known operators first (همراه اول، ایرانسل، رایتل، مخابرات), then the networks that measured the most nodes. Rows keep
 * the node order by name (a colour follows its node, not its rank). */
export function buildHeatmap(targets: ProbeTarget[]): { columns: HeatColumn[]; rows: HeatRow[] } {
  const cols = new Map<string, HeatColumn & { seen: number }>();
  for (const t of targets) {
    // "seen" counts nodes, not measurements: a network listed twice for one node counts once.
    for (const key of new Set(t.networks.map(networkKey))) {
      const n = t.networks.find((x) => networkKey(x) === key);
      if (!n) continue;
      const c = cols.get(key);
      if (c) c.seen += 1;
      else cols.set(key, { key, label: operatorName(n.asn) ?? n.network, network: n.network, asn: n.asn, seen: 1 });
    }
  }
  const columns = [...cols.values()]
    .sort(
      (a, b) =>
        (operatorRank(a.asn) ?? Infinity) - (operatorRank(b.asn) ?? Infinity) ||
        b.seen - a.seen ||
        a.label.localeCompare(b.label, "fa"),
    )
    .map(({ key, label, network, asn }) => ({ key, label, network, asn }));
  const index = new Map(columns.map((c, i) => [c.key, i]));
  const rows = [...targets]
    .sort((a, b) => a.name.localeCompare(b.name, "en"))
    .map((target) => {
      const cells: (HeatCell | null)[] = columns.map(() => null);
      for (const n of target.networks) {
        const i = index.get(networkKey(n));
        if (i === undefined) continue;
        const prev = cells[i];
        // The same network twice (two sources): add the counts, keep the newest time.
        const ok = (prev?.ok ?? 0) + n.ok;
        const fail = (prev?.fail ?? 0) + n.fail;
        const at = prev && prev.at > n.at ? prev.at : n.at;
        cells[i] = { ok, fail, at, tone: cellTone(ok, fail) };
      }
      return { target, cells };
    });
  return { columns, rows };
}

export function verdictCounts(targets: ProbeTarget[]): Record<ProbeVerdict, number> {
  const c: Record<ProbeVerdict, number> = { ok: 0, suspect: 0, blocked: 0, unknown: 0 };
  for (const t of targets) {
    if (t.verdict in c) c[t.verdict] += 1;
  }
  return c;
}

/** The probe target of a node, if the summary has one. */
export function targetOf(targets: ProbeTarget[] | undefined, nodeId: string): ProbeTarget | undefined {
  return targets?.find((t) => t.node_id === nodeId);
}
