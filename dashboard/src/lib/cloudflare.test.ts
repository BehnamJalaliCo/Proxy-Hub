import { describe, expect, it } from "vitest";

import { activeZone, cfTokenProblem, checkSummary, deletionLine, noStandbyLeft, poolChangeText, sortZones, ttlText } from "./cloudflare";
import { deleteBlockedText, deleteWaitText, domainVerdictInfo, POOL_CHOICES, POOL_ORDER, poolStateInfo, zoneStatusInfo } from "./labels";
import type { CfPoolSummary, CfZone, DomainCheck } from "./types";

const Z = (id: string, name: string, pool_state: CfZone["pool_state"]): CfZone => ({ id, name, status: "active", pool_state });

describe("Cloudflare token", () => {
  it("accepts a token-shaped string and never echoes a bad one", () => {
    expect(cfTokenProblem("a".repeat(40))).toBeNull();
    expect(cfTokenProblem(` ${"Ab_-9".repeat(8)}\n`)).toBeNull();
    expect(cfTokenProblem("")).toMatch(/Cloudflare/);
    for (const bad of ["short", "has space inside the token value", "Bearer: abc/def+ghi=jklmnopqrstuv"]) {
      const p = cfTokenProblem(bad);
      expect(p).not.toBeNull();
      expect(p).not.toContain(bad);
    }
  });
});

describe("domain pool", () => {
  const zones = [Z("4", "d.com", "retired"), Z("2", "b.com", "standby"), Z("1", "a.com", "active"), Z("3", "c.com", "none"), Z("5", "e.com", "filtered")];

  it("orders zones active, standby, unused, filtered, retired", () => {
    expect(sortZones(zones).map((z) => z.name)).toEqual(["a.com", "b.com", "c.com", "e.com", "d.com"]);
    expect(activeZone(zones)?.name).toBe("a.com");
  });

  it("explains a change before it is sent", () => {
    expect(poolChangeText(Z("2", "b.com", "standby"), "active", zones)).toMatch(/«a.com» اکنون فعال است/);
    expect(poolChangeText(Z("1", "a.com", "active"), "active", zones)).not.toMatch(/اکنون فعال است/);
    expect(poolChangeText(Z("1", "a.com", "active"), "filtered", zones)).toMatch(/دیگر دامنه‌ی فعال نیست/);
    expect(poolChangeText(Z("3", "c.com", "none"), "standby", zones)).toMatch(/آماده‌به‌کار/);
  });

  it("labels states and Cloudflare statuses, unknown ones as they are", () => {
    expect(poolStateInfo("active").tone).toBe("success");
    expect(poolStateInfo("weird").label).toBe("weird");
    expect(zoneStatusInfo("pending").label).toMatch(/nameserver/);
    expect(zoneStatusInfo("brand_new").label).toBe("brand_new");
  });

  it("writes TTLs in Persian", () => {
    expect(ttlText(60)).toBe("۱ دقیقه");
    expect(ttlText(1)).toBe("خودکار");
    expect(ttlText(45)).toBe("۴۵ ثانیه");
  });
});

// --- the automatic pool (D35) --------------------------------------------------------------------

const NOW = new Date("2026-11-04T12:00:00Z"); // 15:30 on 13 Aban 1405 in Tehran

/** Shaped like the real check of youtube.com from Iran (3 of 4 answers were the filter's). */
const check = (extra: Partial<DomainCheck> = {}): DomainCheck => ({
  at: "2026-11-04T11:50:00Z",
  status: "ok",
  verdict: "unknown",
  reasons: ["single_round"],
  probes: 4,
  answered: 4,
  hijacked: 3,
  networks_answered: 4,
  networks_hijacked: 3,
  networks: [
    { network: "همراه اول", asn: 197207, answered: 1, hijacked: 1 },
    { network: "Sefroyek Pardaz Engineering", asn: 48715, answered: 1, hijacked: 0 },
  ],
  ...extra,
});

const pool = (extra: Partial<CfPoolSummary> = {}): CfPoolSummary => ({
  standby_usable: 1,
  in_use: true,
  kill_switch: false,
  delete_grace_hours: 24,
  standby_check_hours: 6,
  ...extra,
});

describe("automatic domain pool", () => {
  it("never offers 'deleted' to the owner but sorts deleted zones last", () => {
    expect(POOL_CHOICES).not.toContain("deleted");
    expect(POOL_ORDER.at(-1)).toBe("deleted");
    const zones = [Z("9", "z-gone.com", "deleted"), Z("1", "a.com", "active"), Z("5", "e.com", "filtered")];
    expect(sortZones(zones).map((z) => z.name)).toEqual(["a.com", "e.com", "z-gone.com"]);
    expect(poolStateInfo("deleted").label).toMatch(/Cloudflare/);
  });

  it("explains filtering and bringing a domain back before it is sent", () => {
    const zones = [Z("1", "a.com", "active"), Z("5", "e.com", "filtered")];
    const toFiltered = poolChangeText(Z("1", "a.com", "active"), "filtered", zones, 24);
    expect(toFiltered).toContain("۲۴ ساعت");
    expect(toFiltered).toMatch(/از Cloudflare پاک میشود/);
    expect(poolChangeText(Z("5", "e.com", "filtered"), "standby", zones)).toMatch(/لغو میشود/);
  });

  it("puts the last check from Iran in plain Persian", () => {
    expect(checkSummary(null)).toEqual({ tone: "neutral", text: "هنوز از ایران سنجیده نشده است." });
    const clean = checkSummary(check({ hijacked: 0, networks_hijacked: 0, verdict: "clear", reasons: [] }));
    expect(clean.tone).toBe("success");
    expect(clean.text).toContain("هر ۴ پاسخ از ۴ شبکه");
    const once = checkSummary(check());
    expect(once.tone).toBe("warning");
    expect(once.text).toContain("۳ پاسخ از ۴ (از ۳ شبکه)");
    expect(once.text).toMatch(/دوباره سنجیده میشود/);
    expect(checkSummary(check({ verdict: "filtered", reasons: [] })).tone).toBe("danger");
    const failed = checkSummary(check({ status: "rate_limited", answered: 0, hijacked: 0, networks_answered: 0, networks_hijacked: 0 }));
    expect(failed.tone).toBe("neutral");
    expect(failed.text).toContain("سقف ساعتی Globalping");
    expect(domainVerdictInfo("filtered").tone).toBe("danger");
    expect(domainVerdictInfo("odd").label).toBe("odd");
  });

  it("dates the deletion of a filtered domain in Jalali, or says why it waits", () => {
    expect(deletionLine(Z("1", "a.com", "active"), NOW)).toBeNull();
    const filtered: CfZone = {
      ...Z("5", "e.com", "filtered"),
      filtered_at: "2026-11-04T08:30:00Z",
      delete_after: "2026-11-05T08:30:00Z",
      delete_waiting: [],
    };
    const line = deletionLine(filtered, NOW);
    expect(line?.tone).toBe("danger");
    expect(line?.text).toContain("۱۴۰۵/۰۸/۱۴، ۱۲:۰۰"); // 08:30 UTC is 12:00 in Tehran
    expect(line?.text).toMatch(/از Cloudflare پاک میشود/);
    const waiting = deletionLine({ ...filtered, delete_after: "2026-11-04T08:30:00Z", delete_waiting: ["kill_switch_on", "records_remaining"] }, NOW);
    expect(waiting?.text).toMatch(/هنوز پاک نمیشود/);
    expect(waiting?.notes).toEqual([deleteWaitText("kill_switch_on"), deleteWaitText("records_remaining")]);
    expect(waiting?.notes[0]).toMatch(/کلید توقف/);
    const due = deletionLine({ ...filtered, delete_after: "2026-11-04T08:30:00Z" }, NOW);
    expect(due?.text).toMatch(/چند دقیقه‌ی آینده/);
    const blocked = deletionLine({ ...filtered, delete_blocked: "zone_edit_denied" }, NOW);
    expect(blocked?.text).toBe(deleteBlockedText("zone_edit_denied"));
    expect(blocked?.text).toMatch(/\u2066Zone\u00a0·\u00a0Zone\u00a0·\u00a0Edit\u2069/); // one isolated, unbreakable run
    expect(deleteBlockedText("cf_refused:1001")).toMatch(/Registrar/);
    expect(deleteBlockedText("brand_new")).toBe("brand_new");
    expect(deleteWaitText("brand_new")).toBe("brand_new");
    const gone = deletionLine({ ...Z("9", "z.com", "deleted"), deleted_at: "2026-11-03T08:30:00Z" }, NOW);
    expect(gone?.text).toContain("۱۴۰۵/۰۸/۱۲");
    expect(gone?.tone).toBe("neutral");
  });

  it("warns about a missing standby only once the pool is in use", () => {
    expect(noStandbyLeft({ pool: pool({ standby_usable: 0 }) })).toBe(true);
    expect(noStandbyLeft({ pool: pool({ standby_usable: 2 }) })).toBe(false);
    expect(noStandbyLeft({ pool: pool({ standby_usable: 0, in_use: false }) })).toBe(false);
    expect(noStandbyLeft({})).toBe(false); // an older panel without the pool summary
  });
});
