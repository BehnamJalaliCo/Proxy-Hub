import { describe, expect, it } from "vitest";

import {
  captionErrors,
  captionLength,
  durationText,
  KIND_LABEL,
  mediaUrl,
  sizeText,
  SORT_LABEL,
  sourceHealth,
  sourceInputError,
  STATUS_INFO,
  telegramName,
  viewsText,
  type ContentSource,
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
    ]) {
      expect(actionLabel(code)).toMatch(PERSIAN);
    }
  });
});
