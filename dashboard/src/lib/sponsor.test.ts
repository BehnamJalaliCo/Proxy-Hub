import { describe, expect, it } from "vitest";

import { actionLabel } from "./labels";
import {
  campaignBody,
  campaignStateInfo,
  canDelete,
  channelText,
  canEdit,
  draftFromCampaign,
  emptyDraft,
  fromLocalInput,
  lastErrorText,
  leadText,
  moduleStatus,
  parseChannel,
  priceText,
  regCounts,
  regStateInfo,
  sortRegistrations,
  sponsorLine,
  toLocalInput,
  type CampaignDraft,
  type SponsorCampaign,
  type SponsorOverview,
  type SponsorRegistration,
} from "./sponsor";

const NOW = new Date("2026-11-04T12:00:00Z");

const reg = (over: Partial<SponsorRegistration> = {}): SponsorRegistration => ({
  id: "r1",
  address: "203.0.113.7:8443",
  host: "203.0.113.7",
  port: 8443,
  node_id: null,
  node_state: "ready",
  in_use: true,
  state: "registered",
  state_reason: null,
  has_tag: true,
  tag_hint: "ecfe…e999",
  sponsor_channel: "ipoxxo",
  wanted_channel: "ipoxxo",
  in_sync: true,
  registered_at: "2026-11-03T12:00:00Z",
  promotion_set_at: "2026-11-03T12:00:00Z",
  visible_from: null,
  attempts: 0,
  next_attempt_at: null,
  last_error: null,
  stuck: null,
  ...over,
});

const campaign = (over: Partial<SponsorCampaign> = {}): SponsorCampaign => ({
  id: "c1",
  customer: "Shop",
  channel: "customer_one",
  link: "https://t.me/customer_one",
  starts_at: "2026-11-05T12:00:00Z",
  ends_at: "2026-11-06T12:00:00Z",
  switch_at: "2026-11-05T11:00:00Z",
  price: "120.50",
  currency: "EUR",
  paid: true,
  state: "scheduled",
  effective: false,
  created_at: null,
  ...over,
});

const overview = (over: Partial<SponsorOverview> = {}): SponsorOverview => ({
  settings: { default_channel: "ipoxxo", lead_minutes: 60 },
  kill_switch: false,
  provider_ready: true,
  account: { state: "connected", connected: true, mtproxybot_ok: true },
  paused: false,
  stuck_count: 0,
  pause_after_stuck: 2,
  pacing: { sent_last_hour: 0, hourly_cap: 30, min_gap_s: 5, hold_until: null },
  wanted_channel: "ipoxxo",
  active_campaign_id: null,
  registrations: [],
  campaigns: [],
  ...over,
});

describe("public channel links", () => {
  it("keeps only the username of a public channel", () => {
    for (const raw of ["https://t.me/IpoXXo", "t.me/ipoxxo/", "@ipoxxo", "ipoxxo", " https://telegram.me/ipoxxo "]) {
      expect(parseChannel(raw)).toEqual({ ok: true, value: "ipoxxo" });
    }
  });

  it("refuses private invite links and malformed names, never echoing them", () => {
    for (const raw of ["https://t.me/+AbCdEf123", "https://t.me/joinchat/AAAA", "+AbCdEf123"]) {
      const p = parseChannel(raw);
      expect(p.ok).toBe(false);
      if (!p.ok) expect(p.error).toMatch(/خصوصی/);
    }
    for (const raw of ["", "ipo", "9ipoxxo", "ipoxxo_", "https://example.com/ipoxxo", "https://t.me/proxy", "a".repeat(33)]) {
      const p = parseChannel(raw);
      expect(p.ok).toBe(false);
      if (!p.ok && raw !== "") expect(p.error).not.toContain(raw);
    }
  });
});

describe("the module's state", () => {
  it("names the first thing that stops it", () => {
    expect(moduleStatus(overview({ kill_switch: true }), NOW).tone).toBe("danger");
    expect(moduleStatus(overview({ kill_switch: true }), NOW).link?.to).toBe("/settings");
    expect(moduleStatus(overview({ provider_ready: false }), NOW).tone).toBe("info");
    const noAccount = moduleStatus(overview({ account: { state: "none", connected: false, mtproxybot_ok: false } }), NOW);
    expect(noAccount.tone).toBe("warning");
    expect(noAccount.link?.to).toBe("/telegram");
    expect(moduleStatus(overview({ account: { state: "connected", connected: true, mtproxybot_ok: false } }), NOW).title).toMatch(/MTProxybot/);
    expect(moduleStatus(overview({ paused: true, stuck_count: 2 }), NOW).text).toMatch(/۲/);
    const held = overview({ pacing: { sent_last_hour: 3, hourly_cap: 30, min_gap_s: 5, hold_until: "2026-11-04T13:00:00Z" } });
    expect(moduleStatus(held, NOW).title).toMatch(/صبر/);
    expect(moduleStatus(held, new Date("2026-11-04T14:00:00Z")).tone).toBe("success");
    expect(moduleStatus(overview(), NOW).tone).toBe("success");
  });

  it("counts registrations and puts the stuck ones first", () => {
    const regs = [
      reg({ id: "a", address: "b:1" }),
      reg({ id: "b", address: "a:1", state: "failed", in_sync: false }),
      reg({ id: "c", state: "retired", in_sync: false }),
      reg({ id: "d", state: "pending", has_tag: false, in_sync: false }),
      reg({ id: "e", in_sync: false, sponsor_channel: null }),
    ];
    expect(regCounts(regs)).toEqual({ live: 4, inSync: 1, pending: 2, failed: 1 });
    expect(sortRegistrations(regs).map((r) => r.id)).toEqual(["b", "d", "e", "a", "c"]);
    expect(regStateInfo("failed").tone).toBe("danger");
    expect(regStateInfo("weird").label).toBe("weird");
  });

  it("says where each proxy's sponsor stands", () => {
    expect(sponsorLine(reg(), NOW)).toEqual({ tone: "success", text: "اسپانسر: \u200e@ipoxxo\u200e" });
    expect(channelText("ipoxxo")).toBe("\u200e@ipoxxo\u200e"); // stays "@ipoxxo" inside Persian text
    expect(channelText(null)).toBe("—");
    expect(sponsorLine(reg({ visible_from: "2026-11-04T12:40:00Z" }), NOW).text).toMatch(/دیده میشود/);
    expect(sponsorLine(reg({ has_tag: false, in_sync: false }), NOW).tone).toBe("info");
    expect(sponsorLine(reg({ in_sync: false, wanted_channel: "customer_one" }), NOW).text).toMatch(/@customer_one.*@ipoxxo/);
    expect(sponsorLine(reg({ state: "retired" }), NOW).tone).toBe("neutral");
    expect(lastErrorText(null)).toBeNull();
    expect(lastErrorText("no_reply")).toMatch(/جواب نداد/);
    expect(lastErrorText("something_new")).toMatch(/something_new/);
  });
});

describe("campaigns", () => {
  const draft = (over: Partial<CampaignDraft> = {}): CampaignDraft => ({
    customer: "",
    channel: "https://t.me/customer_one",
    starts: toLocalInput(new Date(NOW.getTime() + 3_600_000)),
    ends: toLocalInput(new Date(NOW.getTime() + 25 * 3_600_000)),
    price: "۱۲۰٫۵",
    currency: "eur",
    paid: true,
    ...over,
  });

  it("turns the form into the API body", () => {
    const got = campaignBody(draft(), { now: NOW, defaultChannel: "ipoxxo" });
    expect(got.ok).toBe(true);
    if (!got.ok) return;
    expect(got.body).toMatchObject({ customer: null, channel: "https://t.me/customer_one", price: "120.5", currency: "EUR", paid: true });
    expect(new Date(got.body.starts_at).getTime()).toBe(NOW.getTime() + 3_600_000);
    const blank = campaignBody(draft({ price: "", currency: "", customer: " Shop " }), { now: NOW });
    expect(blank.ok && blank.body).toMatchObject({ price: null, currency: null, customer: "Shop" });
  });

  it("explains every wrong field in Persian", () => {
    const bad = campaignBody(
      draft({ channel: "https://t.me/+private", ends: draft().starts, price: "-3", currency: "euro!", customer: "x".repeat(201) }),
      { now: NOW },
    );
    expect(bad.ok).toBe(false);
    if (bad.ok) return;
    expect(Object.keys(bad.errors).sort()).toEqual(["channel", "currency", "customer", "ends", "price"]);
    const past = campaignBody(draft({ starts: toLocalInput(new Date(NOW.getTime() - 3_600_000)) }), { now: NOW });
    expect(!past.ok && past.errors.starts).toMatch(/گذشته/);
    // An active campaign keeps its (past) start.
    expect(campaignBody(draft({ starts: toLocalInput(new Date(NOW.getTime() - 3_600_000)) }), { now: NOW, started: true }).ok).toBe(true);
    const same = campaignBody(draft({ channel: "@ipoxxo" }), { now: NOW, defaultChannel: "ipoxxo" });
    expect(!same.ok && same.errors.channel).toMatch(/پیش‌فرض/);
    expect(campaignBody(draft({ starts: "garbage" }), { now: NOW }).ok).toBe(false);
  });

  it("round-trips local times and builds drafts", () => {
    const iso = "2026-11-05T09:30:00.000Z";
    expect(fromLocalInput(toLocalInput(iso))).toBe(iso);
    expect(fromLocalInput("2026-11-05")).toBeNull();
    expect(toLocalInput("nonsense")).toBe("");
    const d = draftFromCampaign(campaign({ customer: "@customer_one" }));
    expect(d.customer).toBe("");
    expect(d.channel).toBe("https://t.me/customer_one");
    const fresh = emptyDraft(NOW);
    expect(fromLocalInput(fresh.ends)).not.toBeNull();
    expect(new Date(fromLocalInput(fresh.ends) ?? 0).getTime() - new Date(fromLocalInput(fresh.starts) ?? 0).getTime()).toBe(86_400_000);
  });

  it("allows edits on open campaigns and deletion only before the switch", () => {
    expect(canEdit(campaign())).toBe(true);
    expect(canEdit(campaign({ state: "finished" }))).toBe(false);
    expect(canDelete(campaign(), NOW)).toBe(true);
    expect(canDelete(campaign(), new Date("2026-11-05T11:00:00Z"))).toBe(false);
    expect(canDelete(campaign({ state: "active" }), NOW)).toBe(false);
    expect(campaignStateInfo("active").tone).toBe("success");
    expect(priceText("120.50", "EUR")).toBe("۱۲۰٫۵۰ EUR");
    expect(priceText(null, null)).toBe("بی قیمت");
    expect(leadText(60)).toBe("۱ ساعت زودتر");
    expect(leadText(45)).toBe("۴۵ دقیقه زودتر");
    expect(leadText(0)).toMatch(/درست/);
  });

  it("gives the sponsor actions Persian names in the events page", () => {
    expect(actionLabel("sponsor.flow_stuck")).toMatch(/متوقف/);
    expect(actionLabel("sponsor.something_new")).toBe("sponsor.something_new");
  });
});
