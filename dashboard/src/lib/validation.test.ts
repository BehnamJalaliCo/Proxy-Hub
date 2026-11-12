import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import type { FleetSettings } from "./types";
import {
  draftFromSettings,
  isSettingsChanged,
  isTotp,
  LOCATION,
  MAX_BUDGET_EUR,
  normalizeDigits,
  normalizeTotp,
  parseBudget,
  parseIntIn,
  parseSlugList,
  passwordError,
  passwordScore,
  sameSettings,
  SERVER_TYPE,
  settingsFromDraft,
  settingsPutBody,
  usernameError,
  withKillSwitch,
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
  it("stops at the server maximum (MAX_BUDGET_EUR = 1000000)", () => {
    expect(MAX_BUDGET_EUR).toBe(1_000_000);
    expect(parseBudget("1000000")).toEqual({ ok: true, value: "1000000" });
    expect(parseBudget("۱۰۰۰۰۰۰٫۰۰")).toEqual({ ok: true, value: "1000000.00" });
    for (const bad of ["1000000.01", "1000001", "9999999.99"]) {
      const r = parseBudget(bad);
      expect(r.ok, bad).toBe(false);
      if (!r.ok) expect(r.error).toContain("۱٬۰۰۰٬۰۰۰");
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
    expect(parseSlugList(["fsn1", "fsn1", "hel1"], "location", LOCATION)).toEqual({ ok: true, value: ["fsn1", "hel1"] });
    expect(parseSlugList([], "location", LOCATION).ok).toBe(false);
    expect(parseSlugList(["FSN1"], "location", LOCATION).ok).toBe(false);
    expect(parseSlugList(["fsn 1"], "location", LOCATION).ok).toBe(false);
  });

  it("uses the server's exact rule for each field", () => {
    // fleet/settings_store.py: server types ^[a-z][a-z0-9-]{1,39}$, locations ^[a-z]{3}\d?$
    for (const good of ["fsn1", "nbg1", "hel1", "ash", "hil", "sin"]) {
      expect(parseSlugList([good], "location", LOCATION).ok, good).toBe(true);
    }
    for (const bad of ["fsn12", "fs1", "1fsn", "fsn-1", "eu-central"]) {
      expect(parseSlugList([bad], "location", LOCATION).ok, bad).toBe(false);
    }
    for (const good of ["cpx22", "cax11", "ccx13", "cx22", "c".repeat(40)]) {
      expect(parseSlugList([good], "نوع سرور", SERVER_TYPE).ok, good).toBe(true);
    }
    for (const bad of ["1cpx", "c", "CPX22", "cpx_22", "c".repeat(41)]) {
      expect(parseSlugList([bad], "نوع سرور", SERVER_TYPE).ok, bad).toBe(false);
    }
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

describe("a save over a newer change (lost update)", () => {
  const READ = { ...BASE, updated_at: "2026-11-12T08:30:00.123456+00:00" };

  it("sends the stamp the form was read with, never the settings' own", () => {
    const body = settingsPutBody(READ, READ.updated_at);
    expect(body["expected_updated_at"]).toBe("2026-11-12T08:30:00.123456+00:00");
    expect("updated_at" in body).toBe(false);
    expect(body["dry_run"]).toBe(true);
    // Settings never saved: the stamp is null and is sent as null.
    expect(settingsPutBody({ ...BASE, updated_at: null }, null)["expected_updated_at"]).toBeNull();
    // An older panel sent no stamp: nothing extra goes back (its schema forbids unknown fields).
    expect(Object.keys(settingsPutBody(BASE, undefined)).sort()).toEqual(Object.keys(BASE).sort());
  });

  it("recognises the server's refusal", () => {
    expect(isSettingsChanged(new ApiError(409, "settings_changed", "عوض شده"))).toBe(true);
    expect(isSettingsChanged(new ApiError(409, "other", "x"))).toBe(false);
    expect(isSettingsChanged(new ApiError(422, "settings_changed", "x"))).toBe(false);
    expect(isSettingsChanged(new Error("x"))).toBe(false);
  });

  it("keeps the new stamp after this tab switched the kill switch", () => {
    expect(withKillSwitch(READ, { kill_switch: false, updated_at: "2026-11-12T09:00:00+00:00" })).toEqual({
      ...READ,
      kill_switch: false,
      updated_at: "2026-11-12T09:00:00+00:00",
    });
    expect(withKillSwitch(READ, { kill_switch: false })?.updated_at).toBe(READ.updated_at);
    expect(withKillSwitch(undefined, { kill_switch: true })).toBeUndefined();
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
    // The server checks the username only from 4 characters on (auth/passwords.py).
    expect(passwordError("Formalization!2026", "ali")).toBeNull();
    expect(passwordError("my-alix-password-1", "alix")).not.toBeNull();
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
