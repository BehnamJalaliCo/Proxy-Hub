import { describe, expect, it } from "vitest";

import { modeInfo, planNote, shownNode, slotCounts, slotStatusInfo, spareText, type FleetSlot, type SlotNode } from "./fleet";
import { FLEET_ACTION, FLEET_REASON, FLEET_TRANSITION } from "./fleetLabels";
import { actionLabel, nodeEventLabel, reasonLabel, transitionReasonLabel } from "./labels";

const node = (over: Partial<SlotNode> = {}): SlotNode => ({
  id: "n1",
  name: "ph-s1-abcd1234",
  state: "ready",
  location: "fsn1",
  server_type: "cpx02",
  ipv4: "49.12.1.11",
  verdict: "ok",
  verdict_at: "2026-11-04T10:00:00Z",
  blocked: false,
  created_at: "2026-11-01T10:00:00Z",
  ready_at: "2026-11-01T10:05:00Z",
  state_since: "2026-11-01T10:05:00Z",
  retire_reason: null,
  ...over,
});

const slot = (over: Partial<FleetSlot> = {}): FleetSlot => ({
  slot: 1,
  dns_name: "slot1.example.com",
  status: "healthy",
  node: node(),
  replacement: null,
  others: [],
  leaving: [],
  plan: { action: "none", allowed: false, reasons: ["slot_healthy"] },
  ...over,
});

describe("fleet words", () => {
  it("names every mode and slot status in Persian, and shows unknown codes as they are", () => {
    for (const m of ["live", "dry_run", "stopped"]) expect(modeInfo(m).label).not.toBe(m);
    expect(modeInfo("live").tone).toBe("success");
    for (const s of ["healthy", "settling", "starting", "replacing", "unhealthy", "burned", "empty"]) {
      expect(slotStatusInfo(s).label).not.toBe(s);
    }
    expect(slotStatusInfo("future_status").label).toBe("future_status");
  });

  it("feeds the shared labels: actions, reasons, state-change reasons and node events", () => {
    for (const k of Object.keys(FLEET_ACTION)) expect(actionLabel(k)).toBe(FLEET_ACTION[k]);
    expect(reasonLabel("fleet_dry_run")).toBe(FLEET_REASON["fleet_dry_run"]);
    expect(reasonLabel("delegation_lost")).toContain("Cloudflare");
    expect(reasonLabel("kill_switch_on")).toBe("کلید توقف روشن است"); // the shared word still wins
    expect(transitionReasonLabel("fleet_burned")).toBe(FLEET_TRANSITION["fleet_burned"]);
    expect(nodeEventLabel("burned_ip")).not.toBe("burned_ip");
    expect(nodeEventLabel("state")).toBe("تغییر وضعیت");
  });
});

describe("slot helpers", () => {
  it("counts the slots for the tiles", () => {
    const counts = slotCounts({
      slots: [
        slot(),
        slot({ status: "replacing" }),
        slot({ status: "settling" }),
        slot({ status: "burned" }),
        slot({ status: "unhealthy" }),
        slot({ status: "empty", node: null }),
      ],
    });
    expect(counts).toEqual({ healthy: 1, working: 2, trouble: 2, empty: 1 });
  });

  it("explains the loop's decision in Persian", () => {
    expect(planNote(slot(), "live")).toBeNull(); // a healthy slot needs no note
    const requested = slot({ status: "empty", node: null, plan: { action: "create", allowed: true, reasons: [] } });
    expect(planNote(requested, "live")).toBe("ساخت سرور تازه درخواست شد.");
    expect(planNote(requested, "dry_run")).toContain("در حالت واقعی");
    const blocked = slot({
      status: "burned",
      plan: { action: "create", allowed: false, reasons: ["kill_switch_on", "budget_cap_reached"] },
    });
    expect(planNote(blocked, "stopped")).toBe("سرور تازه لازم است، ولی: به سقف بودجه‌ی ماه میرسد");
    expect(planNote(blocked, "live")).toContain("کلید توقف روشن است");
    const waiting = slot({ status: "unhealthy", plan: { action: "none", allowed: false, reasons: ["slot_unhealthy"] } });
    expect(planNote(waiting, "live")).toContain("مهلت ترمیم");
    expect(planNote(slot({ plan: null }), "live")).toBeNull();
  });

  it("shows the slot's server, else the one on its way", () => {
    const r = node({ id: "n2", state: "requested", ipv4: null });
    expect(shownNode(slot())?.id).toBe("n1");
    expect(shownNode(slot({ node: null, replacement: r }))?.id).toBe("n2");
    expect(shownNode(slot({ node: null }))).toBeNull();
  });

  it("describes the single spare", () => {
    const fa = (n: number) => String(n);
    expect(spareText({ spare: { in_use: false, slots: [] }, slots: [] }, fa)).toContain("آزاد");
    expect(spareText({ spare: { in_use: true, slots: [3] }, slots: [] }, fa)).toBe("در کار جایگاه 3.");
    expect(spareText({ spare: { in_use: true, slots: [] }, slots: [] }, fa)).toBe("در کار است.");
  });
});
