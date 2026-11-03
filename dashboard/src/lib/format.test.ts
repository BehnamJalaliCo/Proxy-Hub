import { describe, expect, it } from "vitest";

import {
  decimalToNumber,
  formatDecimal,
  formatDurationSeconds,
  formatEur,
  formatJalali,
  formatNumber,
  formatPercent,
  formatRelative,
  groupSecret,
  tehranDayKey,
  toFaDigits,
} from "./format";

describe("Persian digits and numbers", () => {
  it("converts only the digits", () => {
    expect(toFaDigits("cpx02 1405/08/13")).toBe("cpx۰۲ ۱۴۰۵/۰۸/۱۳");
    expect(toFaDigits(1234567890)).toBe("۱۲۳۴۵۶۷۸۹۰");
  });

  it("formats numbers with Persian separators", () => {
    expect(formatNumber(12345)).toBe("۱۲٬۳۴۵");
    expect(formatNumber(12.345, 2)).toBe("۱۲٫۳۵");
    expect(formatNumber(Number.NaN)).toBe("—");
  });

  it("formats percent the Persian way", () => {
    expect(formatPercent(0.424)).toBe("۴۲٪");
    expect(formatPercent(1.5)).toBe("۱۵۰٪");
  });
});

describe("money from decimal strings (never through float)", () => {
  it("keeps exact digits and pads to two decimals", () => {
    expect(formatDecimal("1234.5")).toBe("۱٬۲۳۴٫۵۰");
    expect(formatDecimal("0.1")).toBe("۰٫۱۰");
    expect(formatDecimal("150")).toBe("۱۵۰٫۰۰");
    expect(formatDecimal("12345678901234567890.99")).toBe("۱۲٬۳۴۵٬۶۷۸٬۹۰۱٬۲۳۴٬۵۶۷٬۸۹۰٫۹۹");
    expect(formatDecimal("007.129")).toBe("۷٫۱۲");
  });

  it("rejects anything that is not a decimal string", () => {
    expect(formatDecimal(null)).toBe("—");
    expect(formatDecimal("1e3")).toBe("—");
    expect(formatDecimal("abc")).toBe("—");
    expect(formatEur(null)).toBe("—");
    expect(formatEur("9.5")).toBe("۹٫۵۰ یورو");
  });

  it("parses decimals only for proportions", () => {
    expect(decimalToNumber("99.50")).toBe(99.5);
    expect(decimalToNumber(null)).toBeNull();
    expect(decimalToNumber("NaN")).toBeNull();
  });
});

describe("Jalali dates in Tehran time", () => {
  // 2026-11-03T20:45Z is 00:15 on 13 Aban 1405 in Tehran (UTC+03:30, no DST since 2022).
  const iso = "2026-11-03T20:45:00Z";

  it("formats the Tehran calendar day, not the UTC one", () => {
    expect(formatJalali(iso, "date")).toBe("۱۴۰۵/۰۸/۱۳");
    expect(formatJalali(iso, "datetime")).toBe("۱۴۰۵/۰۸/۱۳، ۰۰:۱۵");
    expect(formatJalali(iso, "day")).toBe("۱۳ آبان");
    expect(formatJalali(iso, "long")).toBe("چهارشنبه ۱۳ آبان ۱۴۰۵، ساعت ۰۰:۱۵");
    expect(tehranDayKey(iso)).toBe("1405-08-13");
  });

  it("handles missing and invalid values", () => {
    expect(formatJalali(null)).toBe("—");
    expect(formatJalali("not a date")).toBe("—");
  });

  it("says relative times in Persian", () => {
    const now = new Date("2026-11-03T21:00:00Z");
    expect(formatRelative("2026-11-03T20:59:55Z", now)).toBe("همین حالا");
    expect(formatRelative("2026-11-03T20:55:00Z", now)).toBe("۵ دقیقه پیش");
    expect(formatRelative("2026-11-03T18:00:00Z", now)).toBe("۳ ساعت پیش");
  });

  it("formats durations", () => {
    expect(formatDurationSeconds(12.4)).toBe("۱۲ ثانیه");
    expect(formatDurationSeconds(185)).toBe("۳ دقیقه");
    expect(formatDurationSeconds(3 * 3600 + 5 * 60)).toBe("۳ ساعت و ۵ دقیقه");
    expect(formatDurationSeconds(3 * 86400)).toBe("۳ روز");
    expect(formatDurationSeconds(null)).toBe("—");
    expect(formatDurationSeconds(-1)).toBe("—");
  });
});

describe("TOTP secret grouping", () => {
  it("groups by four for manual entry", () => {
    expect(groupSecret("JBSWY3DPEHPK3PXP")).toBe("JBSW Y3DP EHPK 3PXP");
    expect(groupSecret("ABCDE")).toBe("ABCD E");
  });
});
