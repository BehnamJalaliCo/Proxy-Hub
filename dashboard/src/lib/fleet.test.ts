import { describe, expect, it } from "vitest";

import { modeInfo, planNote, shownNode, slotCounts, slotSquares, slotStatusInfo, spareText, type FleetSlot, type SlotNode } from "./fleet";
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

describe("the overview's slot squares", () => {
  it("shows each slot's own state, not one filled square per live node", () => {
    // 9 live nodes, but slot 7 is unhealthy, slot 8 burned with a replacement booting, slot 9 empty.
    const slots = [1, 2, 3, 4, 5, 6].map((n) => ({ slot: n, status: "healthy" }));
    slots.push({ slot: 7, status: "unhealthy" }, { slot: 8, status: "replacing" }, { slot: 9, status: "empty" });
    const squares = slotSquares(9, slots);
    expect(squares.map((s) => s.status)).toEqual([...Array<string>(6).fill("healthy"), "unhealthy", "replacing", "empty"]);
    expect(squares[6]).toMatchObject({ slot: 7, label: "ناسالم", tone: "danger" });
    expect(squares[8]).toMatchObject({ slot: 9, label: "خالی" });
  });

  it("marks a slot the API does not list as empty, and every slot unknown before loading", () => {
    expect(slotSquares(3, [{ slot: 1, status: "healthy" }]).map((s) => s.status)).toEqual(["healthy", "empty", "empty"]);
    expect(slotSquares(2, null).map((s) => s.status)).toEqual(["unknown", "unknown"]);
    expect(slotSquares(50, null, 40)).toHaveLength(40);
    expect(slotSquares(0, [])).toEqual([]);
  });
});

describe("owner alert and executor codes", () => {
  // Every key of DELAY in panel/proxyhub/fleet/alerts.py (CREATE_BLOCKS plus the three after it):
  // a new alert code is added there and here, with its Persian word in fleetLabels.ts.
  const ALERT_CODES = [
    "budget_cap_reached",
    "node_cap_reached",
    "budget_cap_not_set",
    "node_cap_not_set",
    "account_server_limit_reached",
    "no_active_account",
    "account_guard_failed",
    "account_not_usable",
    "no_live_price",
    "no_other_location",
    "spare_in_use",
    "agent_domain_not_set",
    "all_locations_closed",
    "delete_blocked",
    "burned_ip_streak",
    "download_suspect",
  ];

  it("names every owner alert of the fleet in Persian", () => {
    for (const code of ALERT_CODES) expect(reasonLabel(code), code).toMatch(/[آ-ی]/);
  });

  it("names the executor's closing reasons in the timeline, the events and the slot", () => {
    for (const code of ["no_server_found", "server_already_gone", "no_server_requested"]) {
      expect(transitionReasonLabel(code), code).toMatch(/[آ-ی]/);
      expect(reasonLabel(code), code).toMatch(/[آ-ی]/);
    }
    expect(reasonLabel("create_failed")).toBe("هتزنر ساخت را تأیید نکرد");
  });
});
