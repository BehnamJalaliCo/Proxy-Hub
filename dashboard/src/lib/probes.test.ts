import { describe, expect, it } from "vitest";

import { buildHeatmap, cellTone, networkKey, targetOf, verdictCounts } from "./probes";
import type { ProbeTarget } from "./types";

// Shapes as the contract defines them (GET /api/probes/summary "targets").
const TARGETS: ProbeTarget[] = [
  {
    node_id: "n2",
    name: "ph-slot-2",
    ipv4: "203.0.113.2",
    verdict: "blocked",
    networks: [
      { network: "Rightel", asn: 57218, ok: 0, fail: 3, at: "2026-11-03T10:00:00Z" },
      { network: "Arvan", asn: 202468, ok: 1, fail: 1, at: "2026-11-03T10:00:00Z" },
    ],
  },
  {
    node_id: "n1",
    name: "ph-slot-1",
    ipv4: "203.0.113.1",
    verdict: "ok",
    networks: [
      { network: "Arvan", asn: 202468, ok: 3, fail: 0, at: "2026-11-03T10:00:00Z" },
      { network: "MCI", asn: 197207, ok: 2, fail: 0, at: "2026-11-03T10:00:00Z" },
      { network: "MCI", asn: 197207, ok: 1, fail: 1, at: "2026-11-03T10:05:00Z" },
      { network: "check-host ir1", asn: null, ok: 1, fail: 0, at: "2026-11-03T10:00:00Z" },
    ],
  },
];

describe("probe heatmap", () => {
  it("grades a cell by its numbers", () => {
    expect(cellTone(0, 0)).toBe("neutral");
    expect(cellTone(3, 0)).toBe("success");
    expect(cellTone(0, 2)).toBe("danger");
    expect(cellTone(2, 1)).toBe("warning");
  });

  it("keys networks by ASN when there is one", () => {
    expect(networkKey({ network: "x", asn: 44244 })).toBe("AS44244");
    expect(networkKey({ network: "check-host ir1", asn: null })).toBe("net:check-host ir1");
  });

  it("puts known operators first, then the widest-measured networks", () => {
    const { columns } = buildHeatmap(TARGETS);
    expect(columns.map((c) => c.key)).toEqual(["AS57218", "AS197207", "AS202468", "net:check-host ir1"]);
    expect(columns[0]?.label).toBe("رایتل");
    expect(columns[1]?.label).toBe("همراه اول");
    expect(columns[2]?.label).toBe("Arvan");
  });

  it("orders rows by node name and fills each cell (same network twice is summed)", () => {
    const { rows } = buildHeatmap(TARGETS);
    expect(rows.map((r) => r.target.name)).toEqual(["ph-slot-1", "ph-slot-2"]);
    const [slot1, slot2] = rows;
    expect(slot1?.cells[0]).toBeNull(); // slot 1 was not measured from Rightel
    expect(slot1?.cells[1]).toEqual({ ok: 3, fail: 1, at: "2026-11-03T10:05:00Z", tone: "warning" });
    expect(slot2?.cells[0]).toEqual({ ok: 0, fail: 3, at: "2026-11-03T10:00:00Z", tone: "danger" });
    expect(slot2?.cells[3]).toBeNull();
  });

  it("handles an empty summary", () => {
    expect(buildHeatmap([])).toEqual({ columns: [], rows: [] });
  });

  it("counts verdicts and finds a node's target", () => {
    expect(verdictCounts(TARGETS)).toEqual({ ok: 1, suspect: 0, blocked: 1, unknown: 0 });
    expect(targetOf(TARGETS, "n2")?.name).toBe("ph-slot-2");
    expect(targetOf(undefined, "n2")).toBeUndefined();
  });
});
