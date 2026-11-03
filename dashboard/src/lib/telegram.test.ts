import { describe, expect, it } from "vitest";

import {
  activeWindow,
  botLink,
  botTokenProblem,
  covers,
  coversWholeDay,
  crossesMidnight,
  formatCountdown,
  MAX_WINDOWS,
  normalizeChannel,
  normalizeHHMM,
  pairingLink,
  sameSchedule,
  secondsLeft,
  segments,
  startCommand,
  tehranMinutes,
  validateSchedule,
  windowLength,
} from "./telegram";

describe("times of day", () => {
  it("normalises Persian digits and separators to HH:MM", () => {
    expect(normalizeHHMM("۲۱:۳۰")).toBe("21:30");
    expect(normalizeHHMM("9:05")).toBe("09:05");
    expect(normalizeHHMM("09.05")).toBe("09:05");
    expect(normalizeHHMM(" 23:59 ")).toBe("23:59");
  });

  it("rejects what the server rejects", () => {
    for (const bad of ["24:00", "9:5", "12:60", "", "noon", "1230", "-1:00"]) expect(normalizeHHMM(bad)).toBeNull();
  });
});

describe("no-post windows", () => {
  it("measures a normal window and one crossing midnight", () => {
    expect(windowLength({ start: "20:00", end: "22:30" })).toBe(150);
    expect(windowLength({ start: "23:00", end: "01:00" })).toBe(120);
    expect(crossesMidnight({ start: "23:00", end: "01:00" })).toBe(true);
    expect(crossesMidnight({ start: "01:00", end: "23:00" })).toBe(false);
    expect(windowLength({ start: "bad", end: "01:00" })).toBe(0);
  });

  it("covers its minutes with the end excluded, like the server", () => {
    const w = { start: "23:00", end: "01:00" };
    expect(covers(w, 23 * 60)).toBe(true);
    expect(covers(w, 0)).toBe(true);
    expect(covers(w, 59)).toBe(true);
    expect(covers(w, 60)).toBe(false);
    expect(covers(w, 22 * 60 + 59)).toBe(false);
    expect(covers({ start: "10:00", end: "12:00" }, 12 * 60)).toBe(false);
  });

  it("splits a window crossing midnight into two timeline segments", () => {
    expect(segments({ start: "23:00", end: "01:00" })).toEqual([
      { from: 1380, to: 1440 },
      { from: 0, to: 60 },
    ]);
    expect(segments({ start: "22:00", end: "00:00" })).toEqual([{ from: 1320, to: 1440 }]);
    expect(segments({ start: "08:00", end: "09:00" })).toEqual([{ from: 480, to: 540 }]);
    expect(segments({ start: "08:00", end: "08:00" })).toEqual([]);
  });

  it("knows when the windows leave no minute for a post", () => {
    expect(coversWholeDay([{ start: "00:00", end: "12:00" }, { start: "12:00", end: "00:00" }])).toBe(true);
    expect(coversWholeDay([{ start: "06:00", end: "05:59" }])).toBe(false);
    expect(coversWholeDay([{ start: "06:00", end: "05:59" }, { start: "05:59", end: "06:00" }])).toBe(true);
    expect(coversWholeDay([])).toBe(false);
  });

  it("finds the window that covers now in Tehran time (UTC+3:30, no DST)", () => {
    const now = new Date("2026-11-03T20:00:00Z"); // 23:30 in Tehran
    expect(tehranMinutes(now)).toBe(23 * 60 + 30);
    expect(activeWindow([{ start: "23:00", end: "01:00" }], now)).toEqual({ start: "23:00", end: "01:00" });
    expect(activeWindow([{ start: "08:00", end: "09:00" }], now)).toBeNull();
  });
});

describe("schedule validation mirrors the server", () => {
  it("accepts a valid draft and normalises its times", () => {
    const r = validateSchedule([{ start: "۲۳:۰۰", end: "1:00" }], "۹۰");
    expect(r.settings).toEqual({ quiet_windows: [{ start: "23:00", end: "01:00" }], post_interval_min: 90 });
    expect(r.errors.windows).toEqual([null]);
  });

  it("allows no windows and a zero interval", () => {
    expect(validateSchedule([], "0").settings).toEqual({ quiet_windows: [], post_interval_min: 0 });
  });

  it("flags each bad window by its index", () => {
    const r = validateSchedule(
      [
        { start: "10:00", end: "10:00" },
        { start: "25:00", end: "01:00" },
        { start: "08:00", end: "09:00" },
      ],
      "60",
    );
    expect(r.settings).toBeNull();
    expect(r.errors.windows[0]).toMatch(/یکی باشد/);
    expect(r.errors.windows[1]).toMatch(/آغاز/);
    expect(r.errors.windows[2]).toBeNull();
  });

  it("refuses a whole-day cover, too many windows and an interval out of range", () => {
    expect(validateSchedule([{ start: "00:00", end: "12:00" }, { start: "12:00", end: "00:00" }], "10").errors.form).toMatch(/کل شبانه‌روز/);
    const many = Array.from({ length: MAX_WINDOWS + 1 }, (_, i) => ({ start: `${String(i % 24).padStart(2, "0")}:00`, end: `${String(i % 24).padStart(2, "0")}:30` }));
    expect(validateSchedule(many, "10").errors.form).toMatch(/حداکثر/);
    expect(validateSchedule([], "1441").errors.interval).toBeDefined();
    expect(validateSchedule([], "").errors.interval).toBeDefined();
    expect(validateSchedule([], "1440").settings?.post_interval_min).toBe(1440);
  });

  it("compares schedules by value", () => {
    const a = { quiet_windows: [{ start: "23:00", end: "01:00" }], post_interval_min: 60 };
    expect(sameSchedule(a, { quiet_windows: [{ start: "23:00", end: "01:00" }], post_interval_min: 60 })).toBe(true);
    expect(sameSchedule(a, { ...a, post_interval_min: 61 })).toBe(false);
    expect(sameSchedule(a, { ...a, quiet_windows: [] })).toBe(false);
  });
});

describe("channels", () => {
  it("accepts a username, a t.me link and a private channel id", () => {
    expect(normalizeChannel("@ipoxxo")).toEqual({ ok: true, value: "@ipoxxo" });
    expect(normalizeChannel("ipoxxo")).toEqual({ ok: true, value: "@ipoxxo" });
    expect(normalizeChannel("https://t.me/ipoxxo/")).toEqual({ ok: true, value: "@ipoxxo" });
    expect(normalizeChannel("t.me/IPOXXO_test")).toEqual({ ok: true, value: "@IPOXXO_test" });
    expect(normalizeChannel("-1001234567890")).toEqual({ ok: true, value: "-1001234567890" });
    expect(normalizeChannel("-۱۰۰۱۲۳۴۵۶۷۸۹۰")).toEqual({ ok: true, value: "-1001234567890" });
  });

  it("refuses invite links and impossible names with a Persian hint", () => {
    for (const bad of ["https://t.me/+AbCdEf", "t.me/joinchat/xyz", "", "@ab", "@1channel", "@has space", "@کانال"]) {
      const r = normalizeChannel(bad);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/[آ-ی]/);
    }
  });
});

describe("bot token and pairing", () => {
  const token = `123456789:${"A".repeat(35)}`;

  it("checks the BotFather shape and never echoes the token", () => {
    expect(botTokenProblem(token)).toBeNull();
    expect(botTokenProblem(`  ${token}\n`)).toBeNull();
    expect(botTokenProblem("")).toMatch(/BotFather/);
    for (const bad of ["123:short", `${token} extra`, "not a token at all, clearly", `abc:${"A".repeat(35)}`]) {
      const p = botTokenProblem(bad);
      expect(p).not.toBeNull();
      expect(p).not.toContain(bad.trim());
      expect(p).not.toContain("A".repeat(20));
    }
  });

  it("builds bot and deep links only from plain names and codes", () => {
    expect(botLink("ipoxxo_bot")).toBe("https://t.me/ipoxxo_bot");
    expect(botLink("evil.example/x")).toBeNull();
    expect(pairingLink("ipoxxo_bot", "AB3D5FGH")).toBe("https://t.me/ipoxxo_bot?start=AB3D5FGH");
    expect(pairingLink("ipoxxo_bot", "a b")).toBeNull();
    expect(startCommand("AB3D5FGH")).toBe("/start AB3D5FGH");
  });

  it("counts down to the expiry", () => {
    const now = new Date("2026-11-03T20:00:00Z");
    expect(secondsLeft("2026-11-03T20:09:58Z", now)).toBe(598);
    expect(secondsLeft("2026-11-03T19:59:00Z", now)).toBe(0);
    expect(secondsLeft("garbage", now)).toBe(0);
    expect(formatCountdown(598)).toBe("۰۹:۵۸");
    expect(formatCountdown(0)).toBe("۰۰:۰۰");
  });
});
