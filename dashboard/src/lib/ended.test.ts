import { describe, expect, it } from "vitest";

import { ENDED_NODE_STATES, isEndedConfig, isEndedNode, isEndedProxy, splitEnded } from "./ended";

describe("«پایان‌یافته‌ها»: what leaves the main list", () => {
  it("splits in order and keeps every item exactly once", () => {
    const items = [1, 2, 3, 4, 5, 6];
    const { current, ended } = splitEnded(items, (n) => n % 2 === 0);
    expect(current).toEqual([1, 3, 5]);
    expect(ended).toEqual([2, 4, 6]);
    expect(splitEnded([], () => true)).toEqual({ current: [], ended: [] });
  });

  it("keeps nodes in service and on their way in; burned, draining, retiring and deleted ones end", () => {
    for (const state of ["requested", "creating", "booting", "registering", "ready", "unhealthy", "repairing"]) {
      expect(isEndedNode({ state })).toBe(false);
    }
    for (const state of ["burned", "draining", "retiring", "deleted"]) {
      expect(isEndedNode({ state })).toBe(true);
    }
    expect(ENDED_NODE_STATES.size).toBe(4);
    expect(isEndedNode({ state: "something_new" })).toBe(false); // unknown: stays visible
  });

  it("keeps pending and active MTProto proxies; expired and burned end", () => {
    expect(isEndedProxy({ status: "pending" })).toBe(false);
    expect(isEndedProxy({ status: "active" })).toBe(false);
    expect(isEndedProxy({ status: "expired" })).toBe(true);
    expect(isEndedProxy({ status: "burned" })).toBe(true);
  });

  it("keeps active Reality configs; expired, full and burned end", () => {
    expect(isEndedConfig({ status: "active" })).toBe(false);
    for (const status of ["expired", "full", "burned"]) expect(isEndedConfig({ status })).toBe(true);
  });
});
