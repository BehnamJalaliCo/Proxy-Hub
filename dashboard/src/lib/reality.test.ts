import { describe, expect, it } from "vitest";

import {
  draftFrom,
  formatBytes,
  freshEveryHours,
  meterTone,
  parseDraft,
  REALITY_STATUS,
  sameSettings,
  savedMessage,
  TARGET_AUTO_HINT,
  TARGET_HINT,
  sortConfigs,
  usage,
  certificateState,
  TRANSPORT,
  wsBlocker,
  xrayState,
  type RealityCertificate,
  type RealityConfig,
  type RealitySettings,
} from "./reality";

const DEFAULTS: RealitySettings = {
  enabled: true,
  transport: "ws_tls",
  renew: true,
  configs_per_node: 2,
  cap_hours: 48,
  cap_gb: 1000,
  target_mode: "auto",
  target_host: "dl.google.com",
  target_port: 443,
  link_name: null,
};

function config(over: Partial<RealityConfig>): RealityConfig {
  return {
    id: 1,
    key: "reality:1",
    name: "ipoxxo 🇩🇪 1",
    slot: 1,
    node_name: "ph-slot1",
    location: "fsn1",
    host: "slot1.example.com",
    transport: "ws_tls",
    status: "active",
    listed: true,
    created_at: "2026-11-04T00:00:00Z",
    expires_at: "2026-11-05T00:00:00Z",
    ended_at: null,
    remaining_s: 3600,
    data_cap_bytes: 50e9,
    used_bytes: 0,
    remaining_bytes: 50e9,
    url: "vless://x",
    ...over,
  };
}

describe("reality settings form", () => {
  it("round-trips the defaults", () => {
    const { settings, errors } = parseDraft(draftFrom(DEFAULTS));
    expect(errors).toEqual({});
    expect(settings && sameSettings(settings, DEFAULTS)).toBe(true);
  });

  it("accepts Persian digits and an @ before the link name", () => {
    const { settings } = parseDraft({ ...draftFrom(DEFAULTS), cap_gb: "۸۰", link_name: "@ipoxxo" });
    expect(settings?.cap_gb).toBe(80);
    expect(settings?.link_name).toBe("ipoxxo");
  });

  it("mirrors the server's limits", () => {
    const bad = parseDraft({
      ...draftFrom(DEFAULTS),
      cap_hours: "0",
      cap_gb: "abc",
      configs_per_node: "21",
      target_host: "1.2.3.4",
      target_port: "70000",
      link_name: "<b>",
    });
    expect(bad.settings).toBeNull();
    expect(Object.keys(bad.errors).sort()).toEqual(["cap_gb", "cap_hours", "configs_per_node", "link_name", "target_host", "target_port"]);
    expect(parseDraft({ ...draftFrom(DEFAULTS), target_host: "localhost" }).errors.target_host).toBeTruthy();
    expect(parseDraft({ ...draftFrom(DEFAULTS), link_name: "" }).settings?.link_name).toBeNull();
  });

  it("says what a new target does, and only then", () => {
    const moved = savedMessage(DEFAULTS, { ...DEFAULTS, target_host: "www.example.org" });
    expect(moved).toContain("سایت هدف تازه");
    expect(moved).toContain("بی‌درنگ");
    expect(savedMessage(DEFAULTS, { ...DEFAULTS, target_port: 8443 })).toBe(moved);
    expect(savedMessage(DEFAULTS, { ...DEFAULTS, cap_gb: 80 })).toBe("کانفیگ‌های تازه با این عددها ساخته میشوند.");
    expect(TARGET_HINT).toContain("پس از پایان کانفیگ‌های فعلی");
    expect(TARGET_HINT).not.toContain("فقط برای کلیدهای تازه");
  });

  it("keeps the target mode through the form and says what switching it does", () => {
    const manual = parseDraft({ ...draftFrom(DEFAULTS), target_mode: "manual" }).settings;
    expect(manual?.target_mode).toBe("manual");
    expect(manual && sameSettings(manual, DEFAULTS)).toBe(false);
    expect(savedMessage(DEFAULTS, { ...DEFAULTS, target_mode: "manual" })).toContain("دیگر خودکار عوض نمیشود");
    expect(savedMessage({ ...DEFAULTS, target_mode: "manual" }, DEFAULTS)).toContain("همراه اول و رایتل");
    // A panel older than the automatic choice has no target_mode: the owner's target (manual).
    const older: RealitySettings = { ...DEFAULTS };
    delete older.target_mode;
    expect(draftFrom(older).target_mode).toBe("manual");
    expect(TARGET_AUTO_HINT).toContain("خودکار");
  });

  it("keeps the renew switch through the form", () => {
    const off = parseDraft({ ...draftFrom(DEFAULTS), renew: false }).settings;
    expect(off?.renew).toBe(false);
    expect(off && sameSettings(off, DEFAULTS)).toBe(false);
  });

  it("says how often a fresh config comes", () => {
    expect(freshEveryHours(DEFAULTS)).toBe(24);
    expect(freshEveryHours({ cap_hours: 24, configs_per_node: 3 })).toBe(8);
  });
});

describe("reality numbers", () => {
  it("formats bytes in decimal units with Persian digits", () => {
    expect(formatBytes(50e9)).toBe("۵۰ گیگ");
    expect(formatBytes(1.5e12)).toBe("۱٫۵ ترا");
    expect(formatBytes(350e6)).toBe("۳۵۰ مگ");
    expect(formatBytes(12)).toBe("۱۲ بایت");
    expect(formatBytes(-1)).toBe("—");
  });

  it("measures time and volume used", () => {
    const c = config({ used_bytes: 40e9 });
    const u = usage(c, new Date("2026-11-04T06:00:00Z"));
    expect(u.time).toBeCloseTo(0.25);
    expect(u.volume).toBeCloseTo(0.8);
    expect(usage(c, new Date("2026-11-06T00:00:00Z")).time).toBe(1);
    expect(meterTone(0.5)).toBe("success");
    expect(meterTone(0.75)).toBe("warning");
    expect(meterTone(0.95)).toBe("danger");
  });

  it("lists active configs first, soonest to end first", () => {
    const sorted = sortConfigs([
      config({ id: 1, status: "expired", ended_at: "2026-11-03T00:00:00Z" }),
      config({ id: 2, expires_at: "2026-11-05T10:00:00Z" }),
      config({ id: 3, status: "full", ended_at: "2026-11-04T00:00:00Z" }),
      config({ id: 4, expires_at: "2026-11-05T02:00:00Z" }),
    ]);
    expect(sorted.map((c) => c.id)).toEqual([4, 2, 3, 1]);
  });
});

describe("reality words", () => {
  it("has a Persian label and a tone for every state", () => {
    for (const s of ["active", "expired", "full", "burned"] as const) {
      expect(REALITY_STATUS[s].label).toMatch(/[؀-ۿ]/);
    }
    expect(REALITY_STATUS.burned.tone).toBe("danger");
  });

  it("explains a node's Xray state", () => {
    const base = { enabled: true, up: true, version: "26.10.31", uptime_s: 5, target_ok: true, apply_error: null, reported_at: null };
    expect(xrayState(null).tone).toBe("neutral");
    expect(xrayState({ ...base, enabled: false }).tone).toBe("neutral");
    expect(xrayState(base)).toEqual({ label: "Xray فعال است", tone: "success" });
    expect(xrayState({ ...base, up: false }).tone).toBe("warning");
    expect(xrayState({ ...base, target_ok: false }).tone).toBe("danger");
    expect(xrayState({ ...base, target_ok: null })).toEqual({ label: "Xray فعال است؛ سایت هدف هنوز آزموده نشده", tone: "info" });
    expect(xrayState({ ...base, target_ok: null, up: false }).tone).toBe("warning");
    expect(xrayState({ ...base, apply_error: "xray_config_refused" }).label).toContain("نپذیرفت");
    expect(xrayState({ ...base, apply_error: "something_new" }).tone).toBe("danger");
  });
});

describe("transport (WebSocket + TLS)", () => {
  const cert: RealityCertificate = {
    domain: "ipoxxo.example",
    names: ["*.ipoxxo.example"],
    valid: true,
    not_after: "2027-02-01T00:00:00Z",
    issued_at: "2026-11-05T00:00:00Z",
    last_error: null,
    retry_at: null,
  };
  const node = { dns_name: "slot1.ipoxxo.example", dns_own: true, agent_ws_tls: true };

  it("keeps the transport through the form and says what a switch does", () => {
    const back = parseDraft({ ...draftFrom(DEFAULTS), transport: "reality" }).settings;
    expect(back?.transport).toBe("reality");
    expect(back && sameSettings(back, DEFAULTS)).toBe(false);
    expect(savedMessage(DEFAULTS, { ...DEFAULTS, transport: "reality" })).toContain("Reality برمیگردند");
    expect(savedMessage({ ...DEFAULTS, transport: "reality" }, DEFAULTS)).toContain("WebSocket + TLS");
    expect(TRANSPORT.ws_tls.label).toMatch(/[؀-ۿ]/);
    expect(TRANSPORT.reality.short).toBe("Reality");
  });

  it("says why a node has no WebSocket + TLS inbound yet", () => {
    expect(wsBlocker(node, cert)).toBeNull();
    expect(wsBlocker({ ...node, agent_ws_tls: false }, cert)).toContain("۰٫۳٫۰");
    expect(wsBlocker({ ...node, dns_name: null }, cert)).toContain("نام دامنه");
    expect(wsBlocker(node, { ...cert, valid: false })).toContain("گواهی");
    expect(wsBlocker(node, null)).toContain("گواهی");
    expect(wsBlocker({ ...node, dns_own: false }, cert)).toContain("سرور تازه");
  });

  it("names the certificate state", () => {
    expect(certificateState(cert)).toEqual({ label: "گواهی معتبر است", tone: "success" });
    expect(certificateState({ ...cert, valid: false, last_error: "acme_rateLimited" }).tone).toBe("danger");
    expect(certificateState({ ...cert, valid: false }).tone).toBe("info");
    expect(certificateState(null).tone).toBe("neutral");
  });

  it("explains the node's own TLS check", () => {
    const base = { enabled: true, up: true, version: "26.10.31", uptime_s: 5, target_ok: false, apply_error: null, reported_at: null };
    expect(xrayState(base, "ws_tls").label).toContain("گواهی");
    expect(xrayState({ ...base, target_ok: null }, "ws_tls").label).toContain("TLS خود node");
    expect(xrayState(base, "reality").label).toContain("سایت هدف");
  });
});
