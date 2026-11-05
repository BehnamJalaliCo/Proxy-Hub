import { describe, expect, it } from "vitest";

import { actionLabel, actorLabel } from "./labels";
import {
  effectivePoster,
  packErrorText,
  parsePackLink,
  POSTER_LABEL,
  readyText,
  rightsStatus,
  warningText,
  type OperatorView,
} from "./operatorPoster";

const PERSIAN = /[آ-ی]/;

const view = (over: Partial<OperatorView> = {}): OperatorView => ({
  poster: "operator",
  emoji_pack: "ProxyHubAnimated",
  pack_link: "https://t.me/addemoji/ProxyHubAnimated",
  account: { state: "connected", first_name: "Operator", kill_switch: false },
  hold_until: null,
  channels: {
    test: { username: "@proxyhub_test_ch", public: true, ready: null, rights: null, warnings: [] },
    main: { username: "-1001111111111", public: false, ready: "no_username", rights: null, warnings: [] },
  },
  rights_checked_at: null,
  pack: null,
  fallbacks: [],
  ...over,
});

describe("pack link (mirrors panel/proxyhub/posts/operator.py)", () => {
  it.each([
    ["https://t.me/addemoji/ProxyHub_Anim", "ProxyHub_Anim"],
    ["t.me/addemoji/abc", "abc"],
    ["  https://www.t.me/addemoji/abc/ ", "abc"],
    ["https://telegram.me/addemoji/abc", "abc"],
    ["tg://addemoji?set=abc", "abc"],
    ["HTTPS://T.ME/ADDEMOJI/abc", "abc"],
  ])("takes %s", (raw, name) => {
    expect(parsePackLink(raw)).toEqual({ ok: true, shortName: name });
  });

  it("an empty field removes the pack", () => {
    expect(parsePackLink("   ")).toEqual({ ok: true, shortName: null });
  });

  it.each([
    "https://t.me/addstickers/abc",
    "tg://addstickers?set=abc",
    "https://t.me/addemoji/",
    "https://t.me/addemoji/a.b",
    "https://t.me/addemoji/a/b",
    "https://t.me.evil.example/addemoji/abc",
    "https://evil.example/t.me/addemoji/abc",
    "javascript:alert(1)",
    "abc",
    `https://t.me/addemoji/${"a".repeat(65)}`,
  ])("refuses %s in Persian", (raw) => {
    const got = parsePackLink(raw);
    expect(got.ok).toBe(false);
    if (!got.ok) expect(got.error).toMatch(PERSIAN);
  });

  it("names a sticker pack's link as such", () => {
    const got = parsePackLink("https://t.me/addstickers/abc");
    expect(!got.ok && got.error.includes("استیکر")).toBe(true);
  });
});

describe("words", () => {
  it("the two posters", () => {
    expect(POSTER_LABEL).toEqual({ bot: "ربات", operator: "حساب دوم (Premium)" });
  });

  it("every reason the operator cannot post says the bot takes the post", () => {
    expect(readyText(null)).toBeNull();
    for (const code of ["kill_switch", "not_connected", "no_username", "hold", "no_post_right", "something_new"]) {
      const text = readyText(code) ?? "";
      expect(text).toMatch(PERSIAN);
      expect(text).toContain("ربات");
    }
  });

  it("warnings about the account's rights", () => {
    expect(warningText("delete_right")).toContain("Delete messages");
    expect(warningText("no_post_right")).toContain("Post messages");
    expect(warningText("no_edit_right")).toContain("Edit messages");
    expect(warningText("x")).toBe("x");
  });

  it("rights status", () => {
    expect(rightsStatus(null)).toEqual({ label: "هنوز بررسی نشده", tone: "neutral" });
    const base = { can_post: true, can_edit: true, can_delete: false, error: null, telegram_error: null };
    expect(rightsStatus({ ...base, status: "admin" }).tone).toBe("success");
    expect(rightsStatus({ ...base, status: "not_member" }).tone).toBe("danger");
    expect(rightsStatus({ ...base, status: "member" }).tone).toBe("danger");
    expect(rightsStatus({ ...base, status: "no_username" }).tone).toBe("warning");
    expect(rightsStatus({ ...base, status: "error" }).tone).toBe("warning");
  });

  it("pack errors", () => {
    expect(packErrorText(null)).toBeNull();
    expect(packErrorText("telegram_refused", "STICKERSET_INVALID")).toContain("پیدا نکرد");
    expect(packErrorText("not_emoji_pack")).toContain("ایموجی");
    expect(packErrorText("weird")).toContain("weird");
  });

  it("events of the operator poster have Persian names", () => {
    for (const action of ["posts.poster_fallback", "posts.operator_set", "posts.operator_not_sent", "posts.operator_send_unknown"]) {
      expect(actionLabel(action)).toMatch(PERSIAN);
    }
    expect(actorLabel("operator_poster")).toMatch(PERSIAN);
  });
});

describe("who takes the next new post", () => {
  it("the bot until the owner switches", () => {
    expect(effectivePoster(view({ poster: "bot" }), "test")).toBe("bot");
  });

  it("the second account where it is ready, the bot where it is not", () => {
    expect(effectivePoster(view(), "test")).toBe("operator");
    expect(effectivePoster(view(), "main")).toBe("bot");
  });
});
