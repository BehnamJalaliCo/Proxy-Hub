import { describe, expect, it } from "vitest";

import {
  cleanSummary,
  confirmMatches,
  CONFIRM_TEXT,
  keptLines,
  latestOnly,
  MIN_AGE_OPTIONS,
  normalizeConfirm,
  previewFor,
  purgeLines,
  type EndedPurge,
} from "./cleanup";

describe("«پاک کردن پایان‌یافته‌ها»: the preview belongs to the selected age", () => {
  const preview = (hours: number, total: number): EndedPurge => ({
    dry_run: true,
    min_age_hours: hours,
    cutoff: "2026-11-12T08:00:00Z",
    removed: { nodes: total },
    total,
    kept: {},
    ip_history_unlinked: 0,
  });

  it("never labels or enables a purge with the counts of another age", () => {
    expect(previewFor(preview(168, 3), 1)).toBeNull();
    expect(previewFor(preview(1, 300), 1)?.total).toBe(300);
    expect(previewFor(null, 1)).toBeNull();
  });

  it("applies only the newest of two answers that arrive out of order", async () => {
    const gate = latestOnly();
    const applied: number[] = [];
    let loading = true;
    const settle = (token: number, total: number) => {
      if (!gate.isCurrent(token)) return;
      applied.push(total);
      loading = false;
    };
    let resolveOld: (n: number) => void = () => undefined;
    let resolveNew: (n: number) => void = () => undefined;
    const old = new Promise<number>((r) => (resolveOld = r)); // 168 h, asked first
    const fresh = new Promise<number>((r) => (resolveNew = r)); // 1 h, asked second
    const t1 = gate.next();
    const p1 = old.then((n) => settle(t1, n));
    const t2 = gate.next();
    const p2 = fresh.then((n) => settle(t2, n));
    resolveOld(3);
    await p1;
    expect(applied).toEqual([]); // the older answer neither shows nor ends the loading
    expect(loading).toBe(true);
    resolveNew(300);
    await p2;
    expect(applied).toEqual([300]);
    expect(loading).toBe(false);
    const t3 = gate.next();
    gate.cancel(); // the dialog closed
    expect(gate.isCurrent(t3)).toBe(false);
  });
});

describe("«پاک کردن پایان‌یافته‌ها»: the counts as Persian lines", () => {
  const removed = {
    reality_usage: 2,
    reality_configs: 2,
    probe_network_results: 2,
    probe_measurements: 1,
    probe_rounds: 1,
    probe_node_state: 1,
    probe_location_reach: 1,
    agent_node_reports: 2,
    agent_nodes: 1,
    telemt_users: 1,
    mtproto_proxies: 1,
    node_events: 2,
    nodes: 1,
    mtproxy_stats: 2,
    sponsor_flows: 1,
    mtproxy_registrations: 1,
    post_links: 0,
    posts: 0,
    channel_posts: 0,
    outbox: 5,
  };

  it("groups the tables and leaves empty groups out", () => {
    const lines = purgeLines(removed);
    expect(lines.map((l) => [l.key, l.count])).toEqual([
      ["nodes", 1],
      ["mtproto", 1],
      ["reality", 2],
      ["probes", 6],
      ["node_rows", 8],
      ["sponsor", 1],
      ["sponsor_rows", 3],
      ["outbox", 5],
    ]);
    const total = Object.values(removed).reduce((a, b) => a + b, 0);
    expect(lines.reduce((n, l) => n + l.count, 0)).toBe(total); // every row counted exactly once
  });

  it("never loses a table it does not know (a newer server)", () => {
    expect(purgeLines({ nodes: 1, something_new: 4 })).toEqual([
      { key: "nodes", label: "node حذف‌شده", count: 1 },
      { key: "other", label: "ردیف‌های دیگر", count: 4 },
    ]);
    expect(purgeLines({})).toEqual([]);
  });

  it("explains why ended things stay", () => {
    const lines = keptLines({
      nodes: { location_reach: 2, cost_ledger: 1, billed_this_month: 4, new_reason: 1 },
      reality_configs: { in_recent_post: 0 },
      registrations: { proxy_exists: 3 },
    });
    expect(lines).toEqual([
      "۲ node میماند: سنجش‌های هفته‌ی اخیرشان از ایران هنوز برای انتخاب location لازم است.",
      "۱ node میماند: در دفتر هزینه ثبت شده‌اند (دفتر هزینه هرگز پاک نمیشود).",
      "۴ node میماند: هزینه‌ی این ماهشان هنوز در سقف بودجه شمرده میشود؛ از آغاز ماه بعد پاک میشوند.",
      "۱ node میماند: هنوز در استفاده‌اند.",
      "۳ ثبت اسپانسر میماند: پروکسی‌شان هنوز روی nodeی هست.",
    ]);
  });

  it("offers a minimum age of one hour at least", () => {
    expect(MIN_AGE_OPTIONS.map((o) => o.hours)).toEqual([1, 24, 168]);
    expect(MIN_AGE_OPTIONS.every((o) => o.hours >= 1 && o.hours <= 720)).toBe(true);
  });
});

describe("«پاک کردن کانال آزمایشی»: the typed confirmation", () => {
  it("accepts «پاک کن» however it was typed", () => {
    for (const typed of ["پاک کن", "  پاک   کن ", "پاک‌کن", "پاک‌ کن", "پاك کن"]) {
      expect(confirmMatches(typed)).toBe(true);
    }
    expect(normalizeConfirm("پاک‌کن")).toBe(CONFIRM_TEXT);
  });

  it("refuses anything else", () => {
    for (const typed of ["", "پاک", "پاک کن!", "حذف کن", "clean", "پاک کن".repeat(10)]) {
      expect(confirmMatches(typed)).toBe(false);
    }
  });

  it("says what the cleaning did", () => {
    const base = {
      channel: "@t",
      message_ids: { first: 2, last: 612 },
      requests: 7,
      covered: 611,
      refused: 0,
      finished: true,
      stop: null,
      stop_fa: null,
      removed: { channel_posts: 40, posts: 38, post_links: 120, edit_jobs_closed: 1 },
    };
    expect(cleanSummary(base)).toBe("۷ درخواست به تلگرام برای پیام‌های ۲ تا ۶۱۲؛ ۴۰ پست شناخته‌شده‌ی پنل پاک شد.");
    expect(cleanSummary({ ...base, refused: 3, removed: { ...base.removed, channel_posts: 0 } })).toBe(
      "۷ درخواست به تلگرام برای پیام‌های ۲ تا ۶۱۲؛ ۳ پیام را تلگرام اجازه‌ی حذف نداد و ماند.",
    );
  });
});
