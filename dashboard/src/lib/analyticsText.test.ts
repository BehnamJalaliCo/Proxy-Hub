import { describe, expect, it } from "vitest";

import type { AnalyticsSummary, AnalyticsUsage, ChannelHeadline, ServerStat } from "./analytics";
import {
  costSentence,
  countryName,
  dayLabel,
  fleetTrafficShare,
  formatBps,
  formatBytes,
  formatMb,
  formatSigned,
  ispName,
  membersSentence,
  postsSentence,
  regionName,
  sourceLabel,
  sourcesSentence,
  TB,
  tgSeriesName,
  trafficSentence,
  usersSentence,
} from "./analyticsText";

const server = (over: Partial<ServerStat> = {}): ServerStat => ({
  node_id: "n1",
  name: "fsn-1",
  location: "fsn1",
  server_type: "cpx02",
  state: "ready",
  is_test: false,
  month_out_bytes: 3 * TB,
  month_in_bytes: TB / 4,
  period_out_bytes: 3 * TB,
  included_bytes: 20 * TB,
  traffic_pct: 15,
  net_out_bps: 1.5e6,
  cpu_avg: 20,
  cpu_max: 40,
  signal: "ok",
  fresh: true,
  sampled_at: "2026-11-04T10:00:00Z",
  cost_month_eur: "1.04",
  ...over,
});

const summary = (over: Partial<AnalyticsSummary> = {}): AnalyticsSummary => ({
  generated_at: "2026-11-04T10:00:00Z",
  month: { start: "2026-11-01T00:00:00Z", end: "2026-12-01T00:00:00Z", days_covered: 3.4 },
  traffic: {
    month_out_bytes: 3.2 * TB,
    month_in_bytes: 0.3 * TB,
    today_out_bytes: 0.4 * TB,
    avg_day_out_bytes: 0.94 * TB,
    last_month_out_bytes: 0,
    sampling_since: "2026-11-01T00:00:00Z",
    last_sample_at: "2026-11-04T10:00:00Z",
  },
  cost: {
    month_to_date_eur: "12.40",
    traffic_overage_eur: "0.00",
    total_month_to_date_eur: "12.40",
    projected_month_eur: "58.41",
    per_tb_eur: "3.88",
    priced_by_upper_bound: [],
  },
  servers: [server(), server({ node_id: "n2", name: "hel-1", period_out_bytes: 1 * TB, traffic_pct: 5 })],
  warnings: [],
  channel: { main: null, test: null },
  top_sources: [],
  thresholds: { traffic_warn_pct: 80, cpu_warn_pct: 80, cpu_window_min: 60, alerts_enabled: true },
  collector: {},
  ...over,
});

const headline = (over: Partial<ChannelHeadline> = {}): ChannelHeadline => ({
  title: "IPOXXO",
  username: "@ipoxxo",
  members: 12345,
  members_at: "2026-11-04T10:00:00Z",
  joins_today: 52,
  leaves_today: 7,
  net_today: 45,
  joins_7d: 300,
  leaves_7d: 40,
  net_7d: 260,
  joins_30d: 900,
  leaves_30d: 120,
  net_30d: 780,
  tracking_since: "2026-10-01T00:00:00Z",
  ...over,
});

describe("sizes and numbers", () => {
  it("uses Hetzner's terabyte (1024^4)", () => {
    expect(formatBytes(20 * TB)).toBe("۲۰ ترابایت");
    expect(formatBytes(3.2 * TB)).toBe("۳٫۲ ترابایت");
    expect(formatBytes(850 * 1024 ** 3)).toBe("۸۵۰ گیگابایت");
    expect(formatBytes(12.5 * 1024 ** 2)).toBe("۱۲٫۵ مگابایت");
    expect(formatBytes(null)).toBe("—");
    expect(formatMb(2900)).toBe("۲٫۸ گیگابایت");
  });

  it("speaks network speed in bits", () => {
    expect(formatBps(1.5e6)).toBe("۱۲ مگابیت بر ثانیه");
    expect(formatBps(null)).toBe("—");
  });

  it("keeps the sign on signed counts", () => {
    expect(formatSigned(12)).toContain("+۱۲");
    expect(formatSigned(-3)).toContain("−۳");
    expect(formatSigned(0)).toBe("۰");
  });
});

describe("labels", () => {
  it("names sources in Persian", () => {
    expect(sourceLabel({ kind: "tracking", name: "کانال الف" })).toBe("کانال الف");
    expect(sourceLabel({ kind: "link", name: null })).toBe("لینک دعوت بی‌نام");
    expect(sourceLabel({ kind: "folder", name: null })).toBe("لینک پوشه");
    expect(sourceLabel({ kind: "other", name: null })).toBe("مستقیم یا نامعلوم");
  });

  it("names countries, provinces and Iranian networks", () => {
    expect(countryName("IR")).toBe("ایران");
    expect(countryName("other")).toBe("بقیه");
    expect(regionName("Razavi Khorasan")).toBe("خراسان رضوی");
    expect(regionName("Atlantis")).toBe("Atlantis");
    expect(ispName("AS197207")).toBe("همراه اول (AS197207)");
    expect(ispName("AS44244")).toContain("ایرانسل");
    expect(ispName("AS1")).toBe("AS1");
    expect(tgSeriesName("Followers")).toBe("عضو");
    expect(tgSeriesName("Klingon")).toBe("Klingon");
    expect(dayLabel("2026-11-04")).toBe("۱۳ آبان");
  });
});

describe("sentences", () => {
  it("tells the month's traffic with the share of the free traffic", () => {
    const s = summary();
    expect(fleetTrafficShare(s)).toBeCloseTo(10);
    expect(trafficSentence(s)).toBe(
      "این ماه ۳٫۲ ترابایت به کاربران خدمات دادیم؛ ۱۰٪ سقف ترافیک رایگان سرورها؛ روزی به‌طور میانگین ۹۶۳ گیگابایت.",
    );
    expect(trafficSentence(summary({ traffic: { ...s.traffic, sampling_since: null } }))).toContain("هنوز هیچ سروری سنجیده نشده");
  });

  it("tells the cost and the cost per terabyte", () => {
    expect(costSentence(summary())).toBe("هزینه‌ی سرورها در این ماه تا امروز ۱۲٫۴۰ یورو است؛ یعنی هر ترابایت حدود ۳٫۸۸ یورو.");
    const none = summary({ cost: { ...summary().cost, per_tb_eur: null } });
    expect(costSentence(none)).toContain("هنوز ترافیکی نیست");
  });

  it("tells the channel's growth honestly", () => {
    expect(membersSentence(null)).toContain("هنوز در صفحه‌ی تلگرام ثبت نشده");
    expect(membersSentence(headline())).toContain("کانال اصلی الان ۱۲٬۳۴۵ عضو دارد");
    expect(membersSentence(headline())).toContain("۵۲ ورود، ۷ خروج");
    expect(membersSentence(headline({ tracking_since: null }))).toContain("هنوز هیچ ورود یا خروجی ثبت نشده");
    expect(sourcesSentence([])).toBeNull();
    expect(sourcesSentence([{ key: "tracking:1", kind: "tracking", name: "کانال الف", joins: 120, leaves: 9, net: 111 }])).toBe(
      "بیشترین عضو تازه در ۳۰ روز اخیر از «کانال الف» آمد (۱۲۰ نفر، ۹ نفرشان رفته‌اند).",
    );
  });

  it("says when there is no user data yet", () => {
    const empty: AnalyticsUsage = {
      has_data: false,
      min_users: 3,
      days: [],
      by_country: [],
      by_region: [],
      by_asn: [],
      per_user_mb: { day: null, p50: null, p90: null, p99: null, users: 0 },
      heatmap: [],
      proxies: [],
      hourly: [],
    };
    expect(usersSentence(empty)).toContain("هنوز گزارشی از کاربران پروکسی نرسیده");
    const full: AnalyticsUsage = {
      ...empty,
      has_data: true,
      days: [{ day: "2026-11-04", unique_users: 1250, nodes: 1 }],
      by_country: [
        { key: "IR", avg_daily_users: 1200, user_days: 1200, bytes: 1 },
        { key: "other", avg_daily_users: 50, user_days: 50, bytes: 1 },
      ],
      per_user_mb: { day: "2026-11-04", p50: 35.5, p90: 410, p99: 2900, users: 1250 },
    };
    expect(usersSentence(full)).toBe("در روز ۱۳ آبان ۱٬۲۵۰ کاربر یکتا از پروکسی‌ها استفاده کردند؛ ۹۶٪ از ایران؛ نصف کاربران کمتر از ۳۵٫۵ مگابایت در روز مصرف کردند.");
  });

  it("tells post views with the change against the previous period", () => {
    expect(postsSentence(null)).toBeNull();
    const text = postsSentence({
      at: "2026-11-04T10:00:00Z",
      status: "ok",
      participants: 1310,
      period_start: null,
      period_end: null,
      followers: 1310,
      followers_prev: 1180,
      views_per_post: 820,
      views_per_post_prev: 700,
      shares_per_post: 31,
      shares_per_post_prev: 25,
      reactions_per_post: 12,
      reactions_per_post_prev: 9,
      notifications_pct: 30,
      graphs: {},
    });
    expect(text).toBe("هر پست به‌طور میانگین ۸۲۰ بازدید و ۳۱ بازنشر داشت؛ بازدیدها بیشتر از دوره‌ی قبل (۱۷٪)؛ ۳۰٪ اعضا اعلان کانال را روشن نگه داشته‌اند.");
  });
});
