import { describe, expect, it } from "vitest";

import type { FleetSettings } from "./types";
import {
  draftFromSettings,
  isTotp,
  normalizeDigits,
  normalizeTotp,
  parseBudget,
  parseIntIn,
  parseSlugList,
  passwordError,
  passwordScore,
  sameSettings,
  settingsFromDraft,
  usernameError,
} from "./validation";

const BASE: FleetSettings = {
  kill_switch: true,
  dry_run: true,
  budget_cap_eur: null,
  node_cap_total: null,
  creations_per_hour_cap: 3,
  target_slots: 9,
  allowed_server_types: ["cpx02"],
  locations: ["fsn1", "nbg1", "hel1"],
};

describe("digit normalisation", () => {
  it("accepts Persian and Arabic-Indic digits", () => {
    expect(normalizeDigits("۱۲۳٫۵")).toBe("123.5");
    expect(normalizeDigits("٤٥٦")).toBe("456");
    expect(normalizeDigits(" 1٬200 ")).toBe("1200");
  });
});

describe("budget cap", () => {
  it("is null when empty (not set means nothing is created)", () => {
    expect(parseBudget("")).toEqual({ ok: true, value: null });
  });
  it("keeps the decimal as a string", () => {
    expect(parseBudget("۱۵۰")).toEqual({ ok: true, value: "150" });
    expect(parseBudget("099.50")).toEqual({ ok: true, value: "99.50" });
  });
  it("refuses zero, negatives, too many decimals and junk", () => {
    for (const bad of ["0", "0.00", "-5", "1.234", "abc", "1e3", "1,5"]) {
      expect(parseBudget(bad).ok, bad).toBe(false);
    }
  });
});

describe("integer fields", () => {
  it("enforces the schema range", () => {
    expect(parseIntIn("20", 0, 20, false)).toEqual({ ok: true, value: 20 });
    expect(parseIntIn("21", 0, 20, false).ok).toBe(false);
    expect(parseIntIn("", 0, 200, true)).toEqual({ ok: true, value: null });
    expect(parseIntIn("", 0, 200, false).ok).toBe(false);
    expect(parseIntIn("2.5", 0, 200, false).ok).toBe(false);
  });
});

describe("slug lists", () => {
  it("needs at least one valid Hetzner name and removes duplicates", () => {
    expect(parseSlugList(["fsn1", "fsn1", "hel1"], "location")).toEqual({ ok: true, value: ["fsn1", "hel1"] });
    expect(parseSlugList([], "location").ok).toBe(false);
    expect(parseSlugList(["FSN1"], "location").ok).toBe(false);
    expect(parseSlugList(["fsn 1"], "location").ok).toBe(false);
  });
});

describe("settings draft round trip", () => {
  it("returns the same settings when nothing changed", () => {
    const { settings, errors } = settingsFromDraft(draftFromSettings(BASE), BASE);
    expect(errors).toEqual({});
    expect(sameSettings(settings, BASE)).toBe(true);
  });

  it("applies edits, keeps the kill switch from the base and reports bad fields", () => {
    const draft = { ...draftFromSettings(BASE), budget_cap_eur: "۲۰۰", node_cap_total: "10", target_slots: "999" };
    const { settings, errors } = settingsFromDraft(draft, { ...BASE, kill_switch: false });
    expect(settings.budget_cap_eur).toBe("200");
    expect(settings.node_cap_total).toBe(10);
    expect(settings.kill_switch).toBe(false);
    expect(errors.target_slots).toBeDefined();
    expect(settings.target_slots).toBe(9);
  });

  it("compares decimals by value", () => {
    expect(sameSettings({ ...BASE, budget_cap_eur: "150.00" }, { ...BASE, budget_cap_eur: "150" })).toBe(true);
    expect(sameSettings({ ...BASE, locations: ["hel1", "fsn1", "nbg1"] }, BASE)).toBe(true);
    expect(sameSettings({ ...BASE, dry_run: false }, BASE)).toBe(false);
  });
});

describe("login and setup rules", () => {
  it("mirrors the server username rule", () => {
    expect(usernameError("owner")).toBeNull();
    expect(usernameError("ab")).not.toBeNull();
    expect(usernameError("Owner")).not.toBeNull();
    expect(usernameError("a".repeat(33))).not.toBeNull();
  });

  it("asks for 12+ characters without the username", () => {
    expect(passwordError("short", "owner")).not.toBeNull();
    expect(passwordError("my-owner-password-123", "owner")).not.toBeNull();
    expect(passwordError("correct horse battery", "owner")).toBeNull();
  });

  it("scores strength as a hint only", () => {
    expect(passwordScore("")).toBe(0);
    expect(passwordScore("abc")).toBe(1);
    expect(passwordScore("Tr0ub4dor&3-long-enough!")).toBe(4);
  });

  it("normalises TOTP codes", () => {
    expect(normalizeTotp("۱۲۳ ۴۵۶")).toBe("123456");
    expect(normalizeTotp("12-34-56-78")).toBe("123456");
    expect(isTotp("123456")).toBe(true);
    expect(isTotp("12345")).toBe(false);
  });
});
