import { describe, expect, it } from "vitest";

import {
  domainProblem,
  draftFromMtproto,
  endpointText,
  formatBytes,
  linkChanges,
  linkModeOf,
  MODE_LABEL,
  MTPROTO_STATUS,
  mtprotoFromDraft,
  mtprotoStatusInfo,
  normalizeDomain,
  parsePort,
  serverErrors,
  summarize,
  type MtprotoProxy,
  type MtprotoSettings,
} from "./mtproto";

const PERSIAN = /[آ-ی]/;

const DEFAULTS: MtprotoSettings = {
  port: 8443,
  tls_domain: "www.microsoft.com",
  mode_tls: true,
  mode_secure: false,
  mode_classic: false,
  link_mode: "tls",
};

const proxy = (over: Partial<MtprotoProxy> = {}): MtprotoProxy => ({
  key: "mtproto:00000000-0000-0000-0000-000000000001:mt_0000000000000001",
  node_id: "00000000-0000-0000-0000-000000000001",
  node_name: "ph-slot1",
  node_state: "ready",
  is_test: false,
  slot: 1,
  label: "پروکسی ۱",
  host: "slot1.example.com",
  host_kind: "domain",
  port: 8443,
  mode: "tls",
  tls_domain: "www.microsoft.com",
  link: "https://t.me/proxy?server=slot1.example.com&port=8443&secret=ee",
  status: "active",
  connections: 5,
  total_octets: 2048,
  stats_at: "2026-11-04T10:00:00Z",
  live_at: "2026-11-04T09:00:00Z",
  sponsor_tag_set: false,
  ...over,
});

describe("port", () => {
  it("accepts proxy ports, Persian digits included", () => {
    expect(parsePort("8443")).toEqual({ ok: true, value: 8443 });
    expect(parsePort(" ۸۴۴۳ ")).toEqual({ ok: true, value: 8443 });
    expect(parsePort("1")).toEqual({ ok: true, value: 1 });
    expect(parsePort("65535")).toEqual({ ok: true, value: 65535 });
  });
  it.each(["", "0", "65536", "84a3", "-1", "8443.0", "443", "2083", "22", "9090", "9091"])("refuses %j with a Persian hint", (raw) => {
    const r = parsePort(raw);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(PERSIAN);
  });
  it("names why a reserved port is refused", () => {
    const r = parsePort("443");
    expect(!r.ok && r.error.includes("Reality")).toBe(true);
  });
});

describe("TLS domain", () => {
  it("normalises like the server", () => {
    expect(normalizeDomain(" WWW.Apple.COM. ")).toBe("www.apple.com");
    expect(domainProblem(" WWW.Apple.COM. ")).toBeNull();
    expect(domainProblem("www.microsoft.com")).toBeNull();
  });
  it.each(["", "localhost", "203.0.113.5", "::1", "bad domain.com", "a/b.com", "-x.com", "x.c"])("refuses %j", (raw) => {
    const p = domainProblem(raw);
    expect(p).not.toBeNull();
    expect(p).toMatch(PERSIAN);
  });
});

describe("settings draft", () => {
  it("round-trips the defaults", () => {
    const { value, errors } = mtprotoFromDraft(draftFromMtproto(DEFAULTS));
    expect(errors).toEqual({});
    expect(value).toEqual({ port: 8443, tls_domain: "www.microsoft.com", mode_tls: true, mode_secure: false, mode_classic: false });
  });
  it("collects every problem and sends nothing", () => {
    const { value, errors } = mtprotoFromDraft({ port: "443", tls_domain: "1.2.3.4", mode_tls: false, mode_secure: false, mode_classic: false });
    expect(value).toBeNull();
    expect(Object.keys(errors).sort()).toEqual(["modes", "port", "tls_domain"]);
  });
  it("picks the link mode as the server does", () => {
    expect(linkModeOf({ mode_tls: true, mode_secure: true, mode_classic: true })).toBe("tls");
    expect(linkModeOf({ mode_tls: false, mode_secure: true, mode_classic: true })).toBe("secure");
    expect(linkModeOf({ mode_tls: false, mode_secure: false, mode_classic: true })).toBe("classic");
    expect(linkModeOf({ mode_tls: false, mode_secure: false, mode_classic: false })).toBeNull();
  });
  it("warns about changes that alter every published link", () => {
    const base = { port: 8443, tls_domain: "www.microsoft.com", mode_tls: true, mode_secure: false, mode_classic: false };
    expect(linkChanges(DEFAULTS, base)).toEqual([]);
    expect(linkChanges(DEFAULTS, { ...base, mode_secure: true })).toEqual([]); // the link stays ee
    expect(linkChanges(DEFAULTS, { ...base, port: 9443 })).toEqual(["پورت"]);
    expect(linkChanges(DEFAULTS, { ...base, tls_domain: "www.apple.com" })).toEqual(["دامنه‌ی TLS"]);
    expect(linkChanges(DEFAULTS, { ...base, mode_tls: false, mode_secure: true })).toEqual(["نوع لینک"]);
  });
  it("maps the server's rejected fields", () => {
    expect(Object.keys(serverErrors(["port", "tls_domain", "modes", "unknown"])).sort()).toEqual(["modes", "port", "tls_domain"]);
    expect(serverErrors(["mode_tls"]).modes).toMatch(PERSIAN);
  });
});

describe("list", () => {
  it("summarises only active proxies", () => {
    const s = summarize([
      proxy({ connections: 5, sponsor_tag_set: true }),
      proxy({ connections: 7 }),
      proxy({ status: "pending", connections: null }),
      proxy({ status: "expired", connections: 100 }),
    ]);
    expect(s).toEqual({ active: 2, pending: 1, connections: 12, sponsored: 1 });
    expect(summarize([proxy({ connections: null })]).connections).toBeNull();
    expect(summarize([])).toEqual({ active: 0, pending: 0, connections: null, sponsored: 0 });
  });
  it("formats endpoints and bytes", () => {
    expect(endpointText(proxy())).toBe("slot1.example.com:8443");
    expect(endpointText(proxy({ host: null }))).toBe("—");
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    expect(formatBytes(300 * 1024 ** 2)).toBe("300 MB");
  });
  it("has Persian words for every status and mode", () => {
    for (const info of Object.values(MTPROTO_STATUS)) expect(info.label).toMatch(PERSIAN);
    for (const info of Object.values(MODE_LABEL)) expect(info.hint).toMatch(PERSIAN);
    expect(mtprotoStatusInfo("weird")).toEqual({ label: "weird", tone: "neutral", hint: "" });
  });
});
