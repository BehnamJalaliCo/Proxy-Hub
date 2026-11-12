import * as echarts from "echarts";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { EChart } from "../components/charts/EChart";
import { rtlText, type ChartColors } from "./chart";
import { membersOption } from "./chartOptions";
import { formatNumber } from "./format";

const C: ChartColors = {
  fg: "#111",
  muted: "#555",
  faint: "#888",
  surface: "#fff",
  grid: "#eee",
  brand: "#63f",
  brand2: "#1ad",
  brandSoft: "#eef",
  success: "#0a6",
  warning: "#c80",
  danger: "#d34",
  series1: "#27d",
  series2: "#e63",
  series3: "#2aa",
};

/** 15 days of hourly member counts, as GET /api/analytics/series sends them. */
const hourly = Array.from({ length: 361 }, (_, i) => ({ at: new Date(Date.parse("2026-10-28T00:00:00Z") + i * 3_600_000).toISOString(), count: 10_700 + i * 3 }));

describe("charts in a right-to-left page", () => {
  it("keeps a Persian label in Persian order inside the left-to-right chart", () => {
    expect(rtlText("۱۳ آبان")).toBe("⁧۱۳ آبان⁩");
  });

  it("draws every chart left to right (ECharts' text anchors)", () => {
    expect(renderToStaticMarkup(createElement(EChart, { option: {}, label: "x" }))).toContain('dir="ltr"');
  });
});

describe("the members chart", () => {
  it("places hourly points on a time axis with a few day labels, not one category each", () => {
    const option = membersOption(hourly, C);
    const xAxis = option["xAxis"] as { type: string; inverse: boolean };
    expect(xAxis).toMatchObject({ type: "time", inverse: true });
    const series = (option["series"] as { data: [number, number][] }[])[0];
    expect(series?.data).toHaveLength(361);
    expect(series?.data[0]).toEqual([Date.parse(hourly[0]?.at ?? ""), 10_700]);

    const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width: 470, height: 208 });
    chart.setOption(option);
    // RTL: the newest point on the left, the oldest on the right, across the plot.
    const x = (at: string | undefined) => chart.convertToPixel({ xAxisIndex: 0 }, Date.parse(at ?? ""));
    expect(x(hourly.at(-1)?.at)).toBeLessThan(120);
    expect(x(hourly[0]?.at)).toBeGreaterThan(350);
    expect(chart.renderToSVGString().match(/<text/g)?.length ?? 0).toBeLessThanOrEqual(20);
    chart.dispose();
  });

  it("leaves room on the left for the newest count written at the line's end", () => {
    // The end label («۱۱٬۷۸۰») is not counted by containLabel: the grid's own left margin holds it.
    const grid = membersOption(hourly, C)["grid"] as { left: number };
    expect(grid.left).toBe(16 + 8 * formatNumber(11_780).length);
    expect(grid.left).toBeGreaterThanOrEqual(56);
  });
});
