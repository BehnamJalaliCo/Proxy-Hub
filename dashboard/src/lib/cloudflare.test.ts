import { describe, expect, it } from "vitest";

import { activeZone, cfTokenProblem, poolChangeText, sortZones, ttlText } from "./cloudflare";
import { poolStateInfo, zoneStatusInfo } from "./labels";
import type { CfZone } from "./types";

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
