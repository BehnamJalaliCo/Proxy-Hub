import { describe, expect, it } from "vitest";

import { nodeStateInfo, reasonLabel } from "./labels";
import { agentHealth, canRetire, countByState, eventView, liveTestNode, sortEvents, sortNodes } from "./nodes";
import type { NodeInfo } from "./types";

function node(over: Partial<NodeInfo>): NodeInfo {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    name: "ph-test-1",
    slot: null,
    location: "fsn1",
    server_type: "cpx02",
    state: "ready",
    is_test: false,
    ipv4: "203.0.113.10",
    created_at: "2026-11-03T10:00:00Z",
    ready_at: null,
    agent: { registered: true, last_seen_at: null, telemt_version: null, connections: null },
    ...over,
  };
}

describe("node list", () => {
  it("puts the live test node first, then slots in order, deleted nodes last", () => {
    const list = sortNodes([
      node({ id: "d", state: "deleted", slot: 1 }),
      node({ id: "s2", slot: 2 }),
      node({ id: "t", is_test: true }),
      node({ id: "s1", slot: 1 }),
    ]);
    expect(list.map((n) => n.id)).toEqual(["t", "s1", "s2", "d"]);
  });

  it("finds the one live test node and offers retire only where it can make sense", () => {
    expect(liveTestNode([node({ is_test: true, state: "deleted" })])).toBeUndefined();
    expect(liveTestNode([node({ id: "x", is_test: true, state: "booting" })])?.id).toBe("x");
    expect(canRetire({ state: "ready" })).toBe(true);
    expect(canRetire({ state: "requested" })).toBe(true);
    expect(canRetire({ state: "retiring" })).toBe(false);
    expect(canRetire({ state: "deleted" })).toBe(false);
  });

  it("counts states", () => {
    const c = countByState([node({}), node({}), node({ state: "unhealthy" })]);
    expect(c.get("ready")).toBe(2);
    expect(c.get("unhealthy")).toBe(1);
  });

  it("shows an unknown state as it is", () => {
    expect(nodeStateInfo("brand_new_state").label).toBe("brand_new_state");
    expect(nodeStateInfo("ready").label).toBe("آماده");
  });
});

describe("agent health", () => {
  const now = new Date("2026-11-03T12:00:00Z");
  it("grades the last report", () => {
    expect(agentHealth({ registered: false, last_seen_at: null, telemt_version: null, connections: null }, now).label).toBe("ثبت نشده");
    expect(agentHealth({ registered: true, last_seen_at: null, telemt_version: null, connections: null }, now).tone).toBe("warning");
    expect(agentHealth({ registered: true, last_seen_at: "2026-11-03T11:59:00Z", telemt_version: "3.1", connections: 4 }, now).tone).toBe("success");
    expect(agentHealth({ registered: true, last_seen_at: "2026-11-03T11:50:00Z", telemt_version: null, connections: null }, now).tone).toBe("warning");
    expect(agentHealth({ registered: true, last_seen_at: "2026-11-03T10:00:00Z", telemt_version: null, connections: null }, now).tone).toBe("danger");
  });
});

describe("node events", () => {
  it("reads a state change with its reason", () => {
    const v = eventView({ at: "2026-11-03T12:00:00Z", kind: "state", detail: { from: "ready", to: "retiring", reason: "owner_retire", by: "admin:owner" } });
    expect(v.change).toEqual({ from: "آماده", to: "در حال بازنشستگی" });
    expect(v.reasons).toEqual(["به درخواست مالک"]);
    expect(v.fields).toEqual([{ key: "by", label: "انجام‌دهنده", value: "admin:owner" }]);
  });

  it("lists refusal reasons in Persian and keeps unknown keys visible", () => {
    const v = eventView({
      at: "2026-11-03T12:00:00Z",
      kind: "create_refused",
      detail: { reasons: ["kill_switch_on", "something_new"], hetzner_id: 123456, transient: false, extra: { a: 1 } },
    });
    expect(v.change).toBeNull();
    expect(v.reasons).toEqual([reasonLabel("kill_switch_on"), "something_new"]);
    expect(v.fields).toContainEqual({ key: "hetzner_id", label: "شناسه‌ی هتزنر", value: "123456" });
    expect(v.fields).toContainEqual({ key: "transient", label: "گذرا", value: "خیر" });
    expect(v.fields).toContainEqual({ key: "extra", label: "extra", value: '{"a":1}' });
  });

  it("orders events newest first whatever the time format", () => {
    const list = sortEvents([
      { at: "2026-11-03T10:00:00+00:00", kind: "a", detail: {} },
      { at: "2026-11-03T11:00:00Z", kind: "b", detail: {} },
      { at: "2026-11-03T14:00:00+03:30", kind: "c", detail: {} },
    ]);
    // 14:00+03:30 is 10:30Z: newer than "a", older than "b" (a string sort would put it first).
    expect(list.map((e) => e.kind)).toEqual(["b", "c", "a"]);
  });
});
