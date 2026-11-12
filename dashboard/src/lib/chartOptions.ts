// ECharts options that are worth testing on their own (pure: data and colours in, option out).

import { CHART_FONT, rtlText, type ChartColors } from "./chart";
import { formatJalali, formatNumber } from "./format";

const DAY_MS = 86_400_000;

/** The members line on a TIME axis. On a category axis every hourly point was a category with its
 * own label (about 360 for 15 days), and the chart squeezed into a thin strip at the card's edge;
 * a time axis places points by their time and labels a few days only, however many points come.
 * The newest count is written at the line's end (left, RTL), so the grid keeps room on that side. */
export function membersOption(points: { at: string; count: number }[], c: ChartColors): Record<string, unknown> {
  const values = points.map((p) => p.count);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = Math.max(1, Math.round((max - min) * 0.15));
  const label = { color: c.faint, fontSize: 11, fontFamily: CHART_FONT };
  return {
    animationDuration: 700,
    animationEasing: "cubicOut",
    textStyle: { fontFamily: CHART_FONT },
    grid: { top: 12, bottom: 4, left: 16 + 8 * formatNumber(max).length, right: 8, containLabel: true },
    xAxis: {
      type: "time",
      inverse: true, // RTL: the newest is on the left, read from the right
      minInterval: DAY_MS,
      splitNumber: 4,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: c.grid } },
      splitLine: { show: false },
      axisLabel: { ...label, hideOverlap: true, formatter: (v: number) => rtlText(formatJalali(new Date(v), "day")) },
    },
    yAxis: {
      type: "value",
      position: "right",
      splitNumber: 3,
      splitLine: { lineStyle: { color: c.grid, width: 1, type: "solid" } },
      axisLabel: { ...label, formatter: (v: number) => formatNumber(v) },
      min: Math.max(0, min - pad),
      max: max + pad,
      scale: true,
    },
    series: [
      {
        type: "line",
        showSymbol: points.length < 3,
        symbolSize: 8,
        lineStyle: { width: 2, color: c.series1 },
        itemStyle: { color: c.series1, borderColor: c.surface, borderWidth: 2 },
        areaStyle: { color: c.series1, opacity: 0.1 },
        data: points.map((p) => [Date.parse(p.at), p.count]),
        endLabel: {
          show: true,
          color: c.fg,
          fontFamily: CHART_FONT,
          fontWeight: "bold",
          formatter: (p: { value: [number, number] }) => formatNumber(p.value[1]),
        },
      },
    ],
  };
}
