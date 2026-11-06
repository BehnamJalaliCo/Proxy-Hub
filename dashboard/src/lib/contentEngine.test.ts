import { describe, expect, it } from "vitest";

import {
  API_KEY_RE,
  bridgeState,
  captionErrors,
  captionLength,
  CATEGORY_LABEL,
  durationText,
  FLAG_LABEL,
  KIND_LABEL,
  mediaUrl,
  parseCount,
  parseWords,
  QC_NUMBERS,
  qcVerdict,
  qualityText,
  sizeText,
  SORT_LABEL,
  sourceHealth,
  sourceInputError,
  STATUS_INFO,
  telegramName,
  viewsText,
  type Candidate,
  type ContentSource,
  type QcView,
} from "./contentEngine";
import { actionLabel } from "./labels";

const PERSIAN = /[آ-ی]/;

const source = (over: Partial<ContentSource> = {}): ContentSource => ({
  id: 1,
  kind: "telegram",
  ref: "bbcpersian",
  label: "@bbcpersian",
  link: "https://t.me/bbcpersian",
  enabled: true,
  created_at: "2026-11-07T09:00:00Z",
  last_fetch_at: "2026-11-07T09:05:00Z",
  last_ok_at: "2026-11-07T09:05:00Z",
  last_error: null,
  last_error_fa: null,
  failures: 0,
  next_fetch_at: "2026-11-07T09:10:00Z",
  last_count: 20,
  pending: 3,
  disabled_reason: null,
  disabled_reason_fa: null,
  disabled_at: null,
  quality: null,
  ...over,
});

describe("channel names (mirror panel/proxyhub/content/telegram_preview.py)", () => {
  it.each([
    ["@BBCPersian", "bbcpersian"],
    ["bbcpersian", "bbcpersian"],
    ["https://t.me/bbcpersian", "bbcpersian"],
    ["t.me/s/bbcpersian/", "bbcpersian"],
    ["https://telegram.me/Iran_News_1", "iran_news_1"],
  ])("%s -> %s", (raw, name) => {
    expect(telegramName(raw)).toBe(name);
  });

  it.each(["", "@abc", "https://t.me/joinchat/AAAA", "https://t.me/+AbCdEf", "a__b_c", "1channel", "chan_", "https://t.me/proxy"])(
    "refuses %s",
    (raw) => {
      expect(telegramName(raw)).toBeNull();
    },
  );
});

describe("source input", () => {
  it("accepts a channel or an https feed", () => {
    expect(sourceInputError("telegram", "@bbcpersian", "")).toBeNull();
    expect(sourceInputError("rss", "https://feeds.example.com/fa/rss.xml", "خبر")).toBeNull();
  });

  it.each([
    ["telegram", "https://evil.example/x", ""],
    ["rss", "http://news.example/feed", ""],
    ["rss", "https://10.0.0.1/feed", ""],
    ["rss", "https://news.example:8443/feed", ""],
    ["rss", "https://user@news.example/feed", ""],
    ["rss", "https://news.example/feed", "x".repeat(81)],
  ] as const)("refuses %s %s", (kind, ref, label) => {
    const error = sourceInputError(kind, ref, label);
    expect(error).toMatch(PERSIAN);
  });
});

describe("caption", () => {
  it("counts what Telegram counts: the title line (with 📡 when it has no emoji), a blank line, the text", () => {
    expect(captionLength("🔻 خبر", "متن")).toBe("🔻 خبر".length + 2 + 3);
    expect(captionLength("خبر", "")).toBe("📡 خبر".length);
    expect(captionLength("", "متن")).toBe(3);
    expect(captionLength("  دو   فاصله ", "")).toBe("📡 دو فاصله".length);
  });

  it("has a title or a text, within their limits", () => {
    expect(captionErrors("عنوان", "")).toEqual({});
    expect(captionErrors("", "")).toHaveProperty("title");
    expect(captionErrors("x".repeat(121), "")).toHaveProperty("title");
    expect(captionErrors("a", "b".repeat(1001))).toHaveProperty("body");
  });
});

describe("words", () => {
  it("every status, sort and kind has Persian words", () => {
    for (const info of Object.values(STATUS_INFO)) {
      expect(info.label).toMatch(PERSIAN);
      expect(info.hint).toMatch(PERSIAN);
    }
    for (const label of [...Object.values(SORT_LABEL), KIND_LABEL.telegram]) expect(label).toMatch(PERSIAN);
  });

  it("a source's health", () => {
    expect(sourceHealth(source()).tone).toBe("success");
    expect(sourceHealth(source({ enabled: false })).label).toBe("خاموش");
    expect(sourceHealth(source({ last_error: "not_public" })).tone).toBe("danger");
    expect(sourceHealth(source({ last_ok_at: null })).tone).toBe("info");
  });

  it("numbers in Persian", () => {
    expect(viewsText(6790)).toBe("۶٫۸ هزار");
    expect(viewsText(1_200_000)).toBe("۱٫۲ میلیون");
    expect(viewsText(980)).toBe("۹۸۰");
    expect(viewsText(null)).toBeNull();
    expect(durationText(65)).toBe("۱:۰۵");
    expect(sizeText(310 * 1024)).toBe("۳۱۰ کیلوبایت");
    expect(sizeText(2.4 * 1024 * 1024)).toBe("۲٫۴ مگابایت");
  });

  it("media comes from the panel, never from the source", () => {
    expect(mediaUrl(12)).toBe("/api/content/media/12");
  });

  it("the events page names the content engine's actions", () => {
    for (const code of [
      "content.source_added",
      "content.source_updated",
      "content.source_removed",
      "content.candidate_edited",
      "content.candidate_status",
      "posts.caption_links_dropped",
      "posts.media_skipped",
      "content.qc_approved",
      "content.qc_rejected",
      "content.source_auto_disabled",
      "content.owner_alert",
      "content.qc_settings",
      "content.qc_api_key_set",
      "content.qc_api_key_removed",
    ]) {
      expect(actionLabel(code)).toMatch(PERSIAN);
    }
  });
});

const CONFIG: QcView["config"] = {
  mode: "auto",
  backend: "auto",
  bridge_model: "sonnet",
  api_model: "claude-sonnet-5-5",
  bridge_daily_cap: 40,
  api_daily_cap: 40,
  per_slot: 3,
  lead_minutes: 45,
  timeout_s: 150,
  categories: ["politics", "entertainment"],
  block: {
    sexual: true,
    graphic_violence: true,
    hate: true,
    ads_scam: true,
    foreign_branding: true,
    low_quality: true,
    clickbait: true,
  },
  forbidden_words: [],
  source_window: 20,
  source_min: 10,
  source_max_reject_pct: 80,
  alert_after_hours: 6,
};

const view = (over: Partial<QcView> = {}): QcView => ({
  config: CONFIG,
  defaults: CONFIG,
  options: { categories: ["politics"], flags: ["sexual"], api_models: ["claude-sonnet-5-5", "claude-haiku-5-5"] },
  api_key_set: false,
  calls_24h: { bridge: 3, api: 0 },
  decisions_24h: { approve: 2, reject: 1 },
  ready: 1,
  bridge: { ok: true, logged_in: true, version_ok: true, rate_limit: { status: "allowed", utilization: 0.2 } },
  bridge_error_fa: null,
  last_ok_at: null,
  last_outcome: "ok",
  last_outcome_fa: "Claude پاسخ داد.",
  last_outcome_at: null,
  unavailable_since: null,
  unavailable_reason_fa: null,
  slot: { key: null, calls: 0 },
  alerts: [],
  ...over,
});

describe("the content supervisor (D52)", () => {
  it("names every category and flag in Persian", () => {
    for (const label of [...Object.values(CATEGORY_LABEL), ...Object.values(FLAG_LABEL)]) expect(label).toMatch(PERSIAN);
    for (const f of QC_NUMBERS) expect(f.label).toMatch(PERSIAN);
  });

  it("says how Claude can be reached", () => {
    expect(bridgeState(view()).tone).toBe("success");
    expect(bridgeState(view({ bridge: { ok: false, error: "bridge_missing" }, bridge_error_fa: "پل نیست" }))).toEqual({
      label: "پل نیست",
      tone: "danger",
    });
    expect(bridgeState(view({ bridge: { ok: false }, api_key_set: true })).tone).toBe("warning");
    expect(bridgeState(view({ bridge: { ok: true, logged_in: false } })).tone).toBe("danger");
    expect(bridgeState(view({ bridge: { ok: true, logged_in: true, rate_limit: { status: "rejected" } } })).tone).toBe(
      "warning",
    );
    expect(bridgeState(view({ config: { ...CONFIG, backend: "off" } })).tone).toBe("neutral");
    expect(bridgeState(view({ config: { ...CONFIG, backend: "api" } })).tone).toBe("danger");
    expect(bridgeState(view({ bridge: null })).tone).toBe("info");
  });

  it("shows each candidate's verdict", () => {
    const base = { qc: null } as unknown as Candidate;
    expect(qcVerdict(base)).toBeNull();
    const qc = (over: Partial<NonNullable<Candidate["qc"]>>): Candidate =>
      ({
        qc: {
          decision: "approve",
          reasons: [],
          category: null,
          flags: [],
          checks: [],
          checks_fa: [],
          error: null,
          error_fa: null,
          backend: "bridge",
          model: null,
          at: null,
          tries: 1,
          ...over,
        },
      }) as unknown as Candidate;
    expect(qcVerdict(qc({}))).toEqual({ label: "ناظر: تأیید", tone: "success" });
    expect(qcVerdict(qc({ decision: "reject" }))?.tone).toBe("danger");
    expect(qcVerdict(qc({ decision: "reject", backend: "rules" }))?.label).toBe("قاعده: رد");
    expect(qcVerdict(qc({ decision: null, error: "bad_image" }))?.tone).toBe("warning");
  });

  it("a source's quality and the supervisor's reason", () => {
    expect(qualityText(null)).toBeNull();
    expect(qualityText({ decided: 0, rejected: 0 })).toBeNull();
    expect(qualityText({ decided: 20, rejected: 17 })).toBe("ناظر: ۱۷ از ۲۰ مورد آخر رد شد");
    expect(sourceHealth(source({ enabled: false, disabled_reason: "qc_rejects" })).label).toBe("خاموش با ناظر");
  });

  it("reads the owner's numbers, words and key like the server", () => {
    expect(parseCount("۴۰", 0, 500)).toBe(40);
    expect(parseCount(" 3 ", 1, 10)).toBe(3);
    expect(typeof parseCount("0", 1, 10)).toBe("string");
    expect(typeof parseCount("3.5", 1, 10)).toBe("string");
    expect(typeof parseCount("", 1, 10)).toBe("string");
    expect(parseWords("قمار\n کازینو ،قمار,\n\n")).toEqual(["قمار", "کازینو"]);
    expect(API_KEY_RE.test(`sk-ant-api03-${"x".repeat(40)}`)).toBe(true);
    expect(API_KEY_RE.test("sk-live-123")).toBe(false);
  });
});
