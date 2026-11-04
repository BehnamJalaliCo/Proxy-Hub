import { describe, expect, it } from "vitest";

import { bucketByDay, EMPTY_FILTER, filterEvents, resultText, verdictOf } from "./events";
import { accountActions, actionLabel, actorLabel, reasonLabel, stateReasonText } from "./labels";
import { groupPlan } from "./plan";
import type { AuditEvent } from "./types";

const ev = (id: number, at: string, extra: Partial<AuditEvent> = {}): AuditEvent => ({
  id,
  at,
  actor: "fleet",
  action: "create",
  target: `slot-${String(id)}`,
  dry_run: true,
  allowed: false,
  reasons: ["kill_switch_on"],
  result: "would create",
  ...extra,
});

describe("events", () => {
  it("classifies verdicts", () => {
    expect(verdictOf({ allowed: true })).toBe("allowed");
    expect(verdictOf({ allowed: false })).toBe("blocked");
    expect(verdictOf({ allowed: null })).toBe("info");
  });

  it("buckets by Tehran day, oldest first, with empty days", () => {
    const now = new Date("2026-11-03T21:00:00Z"); // 00:30 on 13 Aban in Tehran
    const events = [
      ev(1, "2026-11-03T20:31:00Z"), // 00:01 on 13 Aban in Tehran
      ev(2, "2026-11-03T20:00:00Z", { allowed: true }), // 23:30 on 12 Aban Tehran
      ev(3, "2026-11-02T10:00:00Z", { allowed: null }), // 13:30 on 11 Aban in Tehran
      ev(4, "2026-10-01T10:00:00Z"), // outside the window
    ];
    const days = bucketByDay(events, 3, now);
    expect(days.map((d) => d.key)).toEqual(["1405-08-11", "1405-08-12", "1405-08-13"]);
    expect(days[2]).toMatchObject({ blocked: 1, allowed: 0, info: 0 });
    expect(days[1]).toMatchObject({ blocked: 0, allowed: 1, info: 0 });
    expect(days[0]).toMatchObject({ info: 1, allowed: 0, blocked: 0 });
  });

  it("filters by verdict, mode, actor and text (Persian labels included)", () => {
    const events = [
      ev(1, "2026-11-03T10:00:00Z"),
      ev(2, "2026-11-03T10:00:00Z", { allowed: true, dry_run: false, actor: "admin:owner", action: "settings_update", reasons: ["budget_cap_eur"] }),
    ];
    const label = (e: AuditEvent) => actionLabel(e.action);
    expect(filterEvents(events, { ...EMPTY_FILTER, verdict: "blocked" }, label).map((e) => e.id)).toEqual([1]);
    expect(filterEvents(events, { ...EMPTY_FILTER, mode: "real" }, label).map((e) => e.id)).toEqual([2]);
    expect(filterEvents(events, { ...EMPTY_FILTER, actor: "admin:owner" }, label).map((e) => e.id)).toEqual([2]);
    expect(filterEvents(events, { ...EMPTY_FILTER, text: "تنظیمات" }, label).map((e) => e.id)).toEqual([2]);
    expect(filterEvents(events, { ...EMPTY_FILTER, text: "SLOT-1" }, label).map((e) => e.id)).toEqual([1]);
  });

  it("renders any result type as text", () => {
    expect(resultText(null)).toBe("");
    expect(resultText("ok")).toBe("ok");
    expect(resultText({ a: 1 })).toBe('{"a":1}');
  });
});

describe("labels", () => {
  it("translates known codes and shows unknown ones as they are", () => {
    expect(reasonLabel("kill_switch_on")).toBe("کلید توقف روشن است");
    expect(reasonLabel("budget_cap_eur")).toBe("سقف بودجه‌ی ماه");
    expect(reasonLabel("brand_new_code")).toBe("brand_new_code");
    expect(reasonLabel("valid_minutes:60")).toBe("اعتبار (دقیقه): ۶۰");
    expect(reasonLabel("method:recovery")).toBe("روش: کد بازیابی");
    expect(reasonLabel("odd:thing")).toBe("odd:thing");
    expect(actionLabel("login_failed")).toBe("ورود ناموفق");
    expect(actorLabel("auth")).toBe("سیستم ورود");
    expect(actionLabel("kill_switch_off")).toBe("خاموش کردن کلید توقف");
    expect(actorLabel("admin:owner")).toBe("مالک (owner)");
    expect(actorLabel("account_guard")).toBe("نگهبان حساب");
  });

  it("names every Cloudflare, DNS and probe action and loop in Persian", () => {
    const actions = [
      "cloudflare_token_set",
      "cloudflare_token_check",
      "cloudflare_pool_change",
      "dns_create",
      "dns_update",
      "dns_delete",
      "dns_conflict",
      "dns_forget",
      "probe_verdict",
      "probe_run_requested",
    ];
    for (const a of actions) expect(actionLabel(a)).toMatch(/[آ-ی]/);
    for (const actor of ["dns", "probes", "cli"]) expect(actorLabel(actor)).toMatch(/[آ-ی]/);
  });

  it("names every action, loop and reason of the automatic domain pool in Persian (D35)", () => {
    for (const a of ["domain_verdict", "domain_check_skipped", "domain_no_standby", "cloudflare_zone_delete"]) {
      expect(actionLabel(a)).toMatch(/[آ-ی]/);
    }
    expect(actorLabel("domains")).toBe("چرخه‌ی دامنه‌ها");
    const reasons = [
      // pool moves (cloudflare_pool_change)
      "owner",
      "replaced",
      "auto_new_zone",
      "auto_promote",
      "domain_filtered",
      "replaces_filtered",
      // token checks
      "zones_gone",
      "cf_token_invalid",
      "cf_token_multiple_accounts",
      "cf_permissions_missing",
      // verdicts and why one is unknown
      "filtered",
      "clear",
      "unknown",
      "no_answers",
      "single_round",
      "weak_signal",
      "not_confirmed",
      "rounds_too_close",
      "rounds_too_old",
      // skipped checks, reminders, deletion outcomes
      "globalping_budget_low",
      "no_standby",
      "deleted",
      "already_absent",
      "zone_edit_denied",
      "cf_refused",
      "cf_unavailable",
      "cf_rate_limited",
      "cf_invalid_response",
    ];
    for (const r of reasons) expect(reasonLabel(r), r).toMatch(/[آ-ی]/);
  });

  it("explains account state reasons, with the foreign server count", () => {
    expect(stateReasonText("foreign_servers:2")).toContain("۲ سرور بیگانه");
    expect(stateReasonText("verified")).toContain("وارسی شد");
    expect(stateReasonText("something:else")).toBe("something:else");
    expect(stateReasonText(null)).toBeNull();
  });

  it("offers drain only for active and retire only for draining/locked", () => {
    expect(accountActions("active")).toEqual({ drain: true, retire: false });
    expect(accountActions("draining")).toEqual({ drain: false, retire: true });
    expect(accountActions("locked")).toEqual({ drain: false, retire: true });
    expect(accountActions("retired")).toEqual({ drain: false, retire: false });
  });
});

describe("plan grouping", () => {
  it("merges identical actions and keeps order", () => {
    const groups = groupPlan([
      { action: "create", target: "slot-1", allowed: false, reasons: ["kill_switch_on", "no_active_account"] },
      { action: "create", target: "slot-2", allowed: false, reasons: ["no_active_account", "kill_switch_on"] },
      { action: "delete", target: "node-a", allowed: true, reasons: [] },
      { action: "create", target: "slot-3", allowed: true, reasons: [] },
    ]);
    expect(groups.map((g) => [g.action, g.allowed, g.targets])).toEqual([
      ["create", false, ["slot-1", "slot-2"]],
      ["delete", true, ["node-a"]],
      ["create", true, ["slot-3"]],
    ]);
  });
});
