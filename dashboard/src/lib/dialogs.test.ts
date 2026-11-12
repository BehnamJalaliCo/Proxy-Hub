import { describe, expect, it } from "vitest";

import { ackFor, dismissAllowed, type AckState } from "./dialogs";

describe("the acknowledgement tick of a confirmation", () => {
  it("is cleared when the parent closes the dialog after a confirmed action", () => {
    // Kill switch OFF: opened, ticked, confirmed; KillSwitchControl closes it with setAsk(false).
    let s: AckState = { open: false, ack: false };
    s = ackFor(s, true);
    s = { ...s, ack: true }; // the owner ticks the box
    expect(ackFor(s, true)).toBe(s); // re-renders while open keep the tick
    s = ackFor(s, false); // closed by the parent, not by Radix
    expect(s.ack).toBe(false);
    s = ackFor(s, true); // the next opening
    expect(s).toEqual({ open: true, ack: false });
  });

  it("starts unticked even if the dialog is reopened before a render saw it closed", () => {
    const ticked: AckState = { open: false, ack: true };
    expect(ackFor(ticked, true).ack).toBe(false);
  });
});

describe("closing a sheet while its request runs", () => {
  it("is refused only while busy; opening is always allowed", () => {
    expect(dismissAllowed(true, false)).toBe(false);
    expect(dismissAllowed(false, false)).toBe(true);
    expect(dismissAllowed(true, true)).toBe(true);
    expect(dismissAllowed(false, true)).toBe(true);
  });
});
