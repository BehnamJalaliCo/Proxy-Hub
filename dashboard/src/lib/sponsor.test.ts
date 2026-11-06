import { describe, expect, it } from "vitest";

import { actionLabel } from "./labels";
import {
  botEntryNoteText,
  botEntryStatusInfo,
  botJobInfo,
  botJobOpen,
  botPromotionInfo,
  botRemovalInfo,
  botScreenStage,
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
  sortBotEntries,
  sortRegistrations,
  sponsorLine,
  tagNote,
  toLocalInput,
  type BotListEntry,
  type BotListJob,
  type BotListSection,
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
  tag_on_node: true,
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
    expect(tagNote(reg())).toBeNull();
    expect(tagNote(reg({ has_tag: false, tag_hint: null, tag_on_node: null }))).toBeNull();
    expect(tagNote(reg({ tag_on_node: false }))).toMatch(/روی node/);
    expect(tagNote(reg({ tag_on_node: false, state: "retired" }))).toBeNull();
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
    expect(actionLabel("sponsor.mtproxybot_removed")).toMatch(/حذف پروکسی مرده/);
    expect(actionLabel("sponsor.mtproxybot_observed")).toMatch(/بی زدن دکمه/);
  });
});

describe("@MTProxybot's own list", () => {
  const entry = (over: Partial<BotListEntry> = {}): BotListEntry => ({
    id: "e1",
    address: "t7aae9f0c.ipoxxo.online",
    host: "t7aae9f0c.ipoxxo.online",
    port: null,
    label_shape: "<host> <hex>",
    page: 1,
    listed: 1,
    status: "live",
    exact: true,
    removable: false,
    registration: { address: "t7aae9f0c.ipoxxo.online:8443", state: "registered", state_reason: null, node_state: "ready" },
    sponsor_channel: "ipoxxo",
    menu_read_at: "2026-11-04T11:00:00Z",
    promotion: "ok",
    removal_state: null,
    removal_reason: null,
    removal_reason_fa: null,
    removal_at: null,
    first_seen_at: "2026-11-04T10:00:00Z",
    ...over,
  });

  const job = (over: Partial<BotListJob> = {}): BotListJob => ({
    kind: "cleanup",
    mode: "observe",
    state: "running",
    requested_by: "admin:owner",
    requested_at: "2026-11-04T11:00:00Z",
    started_at: "2026-11-04T11:01:00Z",
    finished_at: null,
    list_read_at: null,
    relisted: false,
    observed: null,
    removed: 0,
    failures: 0,
    reason: null,
    step: null,
    last_error: null,
    reason_fa: null,
    last_error_fa: null,
    ...over,
  });

  const section = (over: Partial<BotListSection> = {}): BotListSection => ({
    removal_enabled: false,
    cleanup_mode: "observe",
    wanted_channel: "ipoxxo",
    job: null,
    list: { read_at: "2026-11-04T11:30:00Z", pages: 1, complete: true, note: null, total: 1 },
    observation: null,
    counts: { live: 1, gone: 0, retired: 0, unknown: 0, removable: 0, promotion_ok: 1, promotion_problem: 0, promotion_unread: 0 },
    entries: [entry()],
    screens: [],
    ...over,
  });

  it("names each status in Persian as the owner asked", () => {
    expect(botEntryStatusInfo("live").label).toBe("فعال");
    expect(botEntryStatusInfo("gone").label).toBe("سرور حذف‌شده");
    expect(botEntryStatusInfo("unknown").label).toBe("ناشناس");
    expect(botEntryStatusInfo("unknown").hint).toMatch(/هرگز/);
    expect(botEntryStatusInfo("weird").label).toBe("weird");
  });

  it("lists the live proxies first, then the dead, retired and unknown ones, in the bot's order", () => {
    const list = [
      entry({ id: "u", status: "unknown" }),
      entry({ id: "g1", status: "gone" }),
      entry({ id: "l", status: "live" }),
      entry({ id: "r", status: "retired" }),
      entry({ id: "g2", status: "gone" }),
    ];
    expect(sortBotEntries(list).map((e) => e.id)).toEqual(["l", "g1", "g2", "r", "u"]);
  });

  it("flags a live proxy that does not show the wanted sponsor", () => {
    expect(botPromotionInfo(entry(), "ipoxxo")).toEqual({ tone: "success", text: "اسپانسر: ‎@ipoxxo‎" });
    const other = botPromotionInfo(entry({ promotion: "other", sponsor_channel: "someone" }), "ipoxxo");
    expect(other?.tone).toBe("danger");
    expect(other?.text).toMatch(/@someone.*@ipoxxo/);
    expect(botPromotionInfo(entry({ promotion: "not_shown", sponsor_channel: null }), "ipoxxo")?.tone).toBe("warning");
    expect(botPromotionInfo(entry({ promotion: "unread" }), "ipoxxo")?.tone).toBe("info");
    expect(botPromotionInfo(entry({ status: "gone", promotion: null }), "ipoxxo")).toBeNull();
  });

  it("says what the cleanup did to an entry", () => {
    expect(botRemovalInfo(entry(), null)).toBeNull();
    expect(botRemovalInfo(entry({ removal_state: "observed" }), null)?.text).toMatch(/چیزی زده نشد/);
    const removed = entry({ status: "gone", removal_state: "removed", removal_at: "2026-11-04T11:40:00Z" });
    expect(botRemovalInfo(removed, "2026-11-04T11:30:00Z")?.tone).toBe("success");
    // The list was read again after the removal and the proxy is still in it.
    expect(botRemovalInfo(removed, "2026-11-04T11:50:00Z")?.text).toMatch(/هنوز در فهرست/);
    const stuck = entry({ removal_state: "stuck", removal_reason_fa: "در صفحه‌ی تأیید دکمه‌ای نبود." });
    expect(botRemovalInfo(stuck, null)).toEqual({ tone: "danger", text: "در صفحه‌ی تأیید دکمه‌ای نبود." });
  });

  it("describes the job in one line", () => {
    expect(botJobInfo(section())).toBeNull();
    expect(botJobOpen(section())).toBe(false);
    const queued = section({ job: job({ state: "queued", kind: "inventory", mode: "look" }) });
    expect(botJobOpen(queued)).toBe(true);
    expect(botJobInfo(queued)?.title).toMatch(/خواندن فهرست.*در صف/);
    const running = section({ job: job({ mode: "remove", removed: 3, last_error_fa: "به تلگرام نرسید." }) });
    expect(botJobInfo(running)?.text).toMatch(/۳ پروکسی/);
    expect(botJobInfo(running)?.text).toMatch(/به تلگرام نرسید/);
    expect(botJobInfo(section({ job: job({ state: "done", reason: "nothing_to_remove" }) }))?.text).toMatch(/مرده‌ای/);
    const stuck = botJobInfo(section({ job: job({ state: "stuck", reason: "no_confirm_button", reason_fa: "دکمه‌ی تأیید نبود." }) }));
    expect(stuck?.tone).toBe("danger");
    expect(stuck?.text).toMatch(/دکمه‌ی تأیید نبود/);
    expect(botJobOpen(section({ job: job({ state: "stuck" }) }))).toBe(false);
    expect(botJobInfo(section({ job: job({ state: "failed", reason_fa: "ربات جواب نداد." }) }))?.text).toBe("ربات جواب نداد.");
  });

  it("names the recorded screens", () => {
    expect(botScreenStage("list_page_2")).toBe("فهرست، صفحه‌ی ۲");
    expect(botScreenStage("proxy_screen")).toMatch(/پروکسی/);
    expect(botScreenStage("after_confirm")).toMatch(/تأیید/);
    expect(botScreenStage("other")).toBe("other");
  });
});

describe("botEntryNoteText", () => {
  it("explains why an entry with no dead row of ours is still removable (P18)", () => {
    expect(botEntryNoteText("old_tag")).toContain("ثبت قدیمی");
    expect(botEntryNoteText("no_record")).toContain("رکوردش پاک شده");
    expect(botEntryNoteText(null)).toBeNull();
    expect(botEntryNoteText(undefined)).toBeNull();
  });
});
