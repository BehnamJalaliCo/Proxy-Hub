import { describe, expect, it } from "vitest";

import {
  blockedText,
  contentStatusInfo,
  decodeEntities,
  excerpt,
  parseTags,
  parseTgHtml,
  plainText,
  postsThatFit,
  sameSettings,
  toDraft,
  validateContent,
  validateSettings,
  visibleLength,
  wrapSelection,
  type PostingSettings,
  type TgNode,
} from "./posts";

const DEFAULTS: PostingSettings = {
  enabled: true,
  main_enabled: false,
  posts_per_day: 10,
  day_start: "08:00",
  day_end: "00:00",
  max_mtproto: 12,
  max_reality: 3,
  edit_window_hours: 48,
  recycle_after_days: 14,
  status_texts: { expired: "⌛️ زمان این کانفیگ تمام شد", full: "🔴 ظرفیت این کانفیگ پر شد", burned: "🚫 این کانفیگ از دسترس خارج شد" },
};

describe("posting settings", () => {
  it("round-trips the server defaults", () => {
    const { settings, errors } = validateSettings(toDraft(DEFAULTS));
    expect(errors).toEqual({});
    expect(settings).toEqual(DEFAULTS);
    expect(settings && sameSettings(settings, DEFAULTS)).toBe(true);
  });

  it("accepts Persian digits and normalises times", () => {
    const d = { ...toDraft(DEFAULTS), posts_per_day: "۱۲", day_start: "۹:۳۰", day_end: "23.45" };
    const { settings } = validateSettings(d);
    expect(settings?.posts_per_day).toBe(12);
    expect(settings?.day_start).toBe("09:30");
    expect(settings?.day_end).toBe("23:45");
  });

  it("refuses what the server refuses", () => {
    const bad = {
      ...toDraft(DEFAULTS),
      posts_per_day: "49",
      max_reality: "6",
      edit_window_hours: "0",
      recycle_after_days: "abc",
      day_start: "25:00",
      status_texts: { expired: "  ", full: "x".repeat(81), burned: "🚫" },
    };
    const { settings, errors } = validateSettings(bad);
    expect(settings).toBeNull();
    expect(Object.keys(errors).sort()).toEqual(
      ["day_start", "edit_window_hours", "expired", "full", "max_reality", "posts_per_day", "recycle_after_days"].sort(),
    );
  });

  it("needs at least one link kind and distinct posting hours", () => {
    expect(validateSettings({ ...toDraft(DEFAULTS), max_mtproto: "0", max_reality: "0" }).errors.max_reality).toBeTruthy();
    expect(validateSettings({ ...toDraft(DEFAULTS), day_start: "09:00", day_end: "09:00" }).errors.day_end).toBeTruthy();
    expect(validateSettings({ ...toDraft(DEFAULTS), posts_per_day: "0" }).settings?.posts_per_day).toBe(0);
  });
});

describe("content checks", () => {
  it("parses tags like the server", () => {
    expect(parseTags("ترفند، #امنیت  Tips tips")).toEqual({ tags: ["ترفند", "امنیت", "tips"], error: null });
    expect(parseTags("حریم‌خصوصی").tags).toEqual(["حریم‌خصوصی"]);
    expect(parseTags("").tags).toEqual([]);
    expect(parseTags("a!b").error).toBeTruthy();
    expect(parseTags("a b c d e f g h i").error).toBeTruthy();
  });

  it("checks title, body and tags", () => {
    const ok = validateContent("  عنوان  تازه ", "متن <b>پررنگ</b>\r\nخط دوم", "ترفند");
    expect(ok.input).toEqual({ title: "عنوان تازه", body: "متن <b>پررنگ</b>\nخط دوم", tags: ["ترفند"] });
    const bad = validateContent("", "<b></b>", "دو,کلمه!");
    expect(bad.input).toBeNull();
    expect(Object.keys(bad.errors).sort()).toEqual(["body", "tags", "title"]);
    expect(validateContent("ع", "ی".repeat(1501), "").errors.body).toContain("۱۵۰۱");
  });
});

describe("Telegram HTML reader", () => {
  it("builds a tree of the tags Telegram shows", () => {
    const nodes = parseTgHtml('<b>عنوان</b>\n\n<blockquote>متن <i>کج</i> &amp; &lt;b&gt;</blockquote>\n<a href="https://t.me/proxy?server=a.com&amp;port=1">پروکسی ۱</a>');
    expect(nodes[0]).toEqual({ type: "el", tag: "b", children: [{ type: "text", text: "عنوان" }] });
    const quote = nodes[2] as Extract<TgNode, { type: "el" }>;
    expect(quote.tag).toBe("blockquote");
    expect(plainText(quote.children)).toBe("متن کج & <b>");
    const link = nodes[4] as Extract<TgNode, { type: "el" }>;
    expect(link.href).toBe("https://t.me/proxy?server=a.com&port=1");
  });

  it("reads a config's code block and its language as Telegram does", () => {
    const nodes = parseTgHtml('⏳ تا ۱۸:۳۰\n<pre><code class="language-XHTTP">vless://a@b:1?x=1&amp;y=2</code></pre>\n<pre>vless://c@d:1</pre>');
    const marked = nodes[1] as Extract<TgNode, { type: "el" }>;
    expect(marked).toEqual({ type: "el", tag: "pre", lang: "XHTTP", children: [{ type: "text", text: "vless://a@b:1?x=1&y=2" }] });
    const plain = nodes[3] as Extract<TgNode, { type: "el" }>;
    expect(plain.tag).toBe("pre");
    expect(plain.lang).toBeUndefined();
    // A code outside a pre stays inline code; a bad language is no language.
    expect((parseTgHtml("<code>x</code>")[0] as Extract<TgNode, { type: "el" }>).tag).toBe("code");
    expect((parseTgHtml('<pre><code class="language-a b">x</code></pre>')[0] as Extract<TgNode, { type: "el" }>).lang).toBeUndefined();
  });

  it("drops unsafe links and unknown tags but keeps their text", () => {
    const nodes = parseTgHtml('<a href="javascript:alert(1)">بد</a><script>x</script><span>y</span><span class="tg-spoiler">z</span>');
    const link = nodes[0] as Extract<TgNode, { type: "el" }>;
    expect(link.href).toBeUndefined();
    expect(plainText(nodes)).toBe("بدxyz");
    expect((nodes.at(-1) as Extract<TgNode, { type: "el" }>).tag).toBe("spoiler");
  });

  it("survives broken markup", () => {
    expect(plainText(parseTgHtml("a < b <b>c</i> d"))).toBe("a < b c d");
    expect(plainText(parseTgHtml("<br>x<br/>"))).toBe("\nx\n");
    expect(decodeEntities("&#1588;&#x634;&unknown;")).toBe("شش&unknown;");
    // Out of range or surrogate references never throw (the live preview must not break).
    expect(decodeEntities("a&#9999999;b&#xFFFFFF;c&#0;d&#xD800;e")).toBe("a\uFFFDb\uFFFDc\uFFFDd\uFFFDe");
    expect(decodeEntities("&#x10FFFF;")).toBe(String.fromCodePoint(0x10ffff));
    expect(() => parseTgHtml("<b>&#9999999;</b>")).not.toThrow();
  });

  it("measures and shortens visible text", () => {
    expect(visibleLength("<b>ab</b> &amp;")).toBe(4);
    expect(excerpt("<b>یک</b>\n\nدو   سه", 140)).toBe("یک دو سه");
    expect(excerpt("ی".repeat(200), 20)).toHaveLength(20);
  });

  it("wraps a selection for the toolbar", () => {
    expect(wrapSelection("سلام دنیا", 5, 9, "<b>", "</b>")).toEqual({ text: "سلام <b>دنیا</b>", start: 8, end: 12 });
    expect(wrapSelection("abc", 3, 3, "<i>", "</i>").text).toBe("abc<i></i>");
  });
});

describe("posts that fit the posting hours", () => {
  it("counts the gaps of the posting hours, also past midnight", () => {
    expect(postsThatFit("08:00", "00:00", 60)).toBe(16);
    expect(postsThatFit("22:00", "02:00", 30)).toBe(8);
    expect(postsThatFit("۰۸:۰۰", "۱۰:۰۰", 120)).toBe(1);
    expect(postsThatFit("08:00", "08:30", 60)).toBe(1);
  });

  it("has no limit without a gap or with hours that are not valid", () => {
    expect(postsThatFit("08:00", "00:00", 0)).toBeNull();
    expect(postsThatFit("08:00", "08:00", 60)).toBeNull();
    expect(postsThatFit("25:00", "08:00", 60)).toBeNull();
  });
});

describe("words", () => {
  it("explains why nothing is posted", () => {
    expect(blockedText(null)).toBeNull();
    for (const code of ["disabled", "no_bot", "no_channel", "no_links", "other"]) expect(blockedText(code)).toBeTruthy();
    expect(contentStatusInfo("approved").tone).toBe("success");
    expect(contentStatusInfo("weird").label).toBe("weird");
  });
});
