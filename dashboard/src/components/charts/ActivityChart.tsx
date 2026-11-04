import { useMemo } from "react";

import { bucketByDay, type DayBucket } from "../../lib/events";
import { formatNumber } from "../../lib/format";
import type { AuditEvent } from "../../lib/types";
import { CHART_FONT, useChartColors } from "../../lib/chart";
import { EChart, type EChartsOption } from "./EChart";

const SERIES: { key: keyof Pick<DayBucket, "allowed" | "info" | "blocked">; name: string; color: "series1" | "series3" | "series2" }[] = [
  { key: "allowed", name: "مجاز / انجام‌شده", color: "series1" },
  { key: "info", name: "گزارشی", color: "series3" },
  { key: "blocked", name: "ردشده با قفل ایمنی", color: "series2" },
];

/** Legend for the activity chart (names only; values are in the tooltip and the list). */
export function ActivityLegend() {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label="راهنمای نمودار">
      {SERIES.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span
            className={
              s.color === "series1" ? "size-2.5 rounded-sm bg-series-1" : s.color === "series2" ? "size-2.5 rounded-sm bg-series-2" : "size-2.5 rounded-sm bg-series-3"
            }
          />
          {s.name}
        </li>
      ))}
    </ul>
  );
}

/** Stacked daily columns of audit events (Tehran days, newest on the left for RTL reading). */
export function ActivityChart({ events, days = 14, className }: { events: AuditEvent[]; days?: number; className?: string }) {
  const c = useChartColors();
  const buckets = useMemo(() => bucketByDay(events, days), [events, days]);

  const option = useMemo<EChartsOption>(() => {
    // Round only the top-most non-empty segment of each column (4px data end, square baseline).
    const topIndex = buckets.map((b) => {
      let top = -1;
      SERIES.forEach((s, i) => {
        if (b[s.key] > 0) top = i;
      });
      return top;
    });
    return {
      animationDuration: 700,
      animationEasing: "cubicOut",
      textStyle: { fontFamily: CHART_FONT },
      grid: { top: 12, bottom: 4, left: 8, right: 8, containLabel: true },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow", shadowStyle: { color: c.brandSoft } },
        backgroundColor: c.surface,
        borderColor: c.grid,
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: c.fg, fontFamily: CHART_FONT, fontSize: 12 },
        extraCssText: "direction: rtl; text-align: right; border-radius: 12px; box-shadow: 0 12px 30px -12px rgba(0,0,0,.35);",
        formatter: (raw: unknown) => {
          const params = (Array.isArray(raw) ? raw : [raw]) as { dataIndex: number }[];
          const b = buckets[params[0]?.dataIndex ?? 0];
          if (!b) return "";
          const rows = SERIES.map(
            (s) =>
              `<div style="display:flex;align-items:center;gap:8px;justify-content:space-between;min-width:150px"><span style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:2px;background:${c[s.color]}"></span>${s.name}</span><b>${formatNumber(b[s.key])}</b></div>`,
          ).join("");
          return `<div style="font-weight:700;margin-bottom:4px">${b.label}</div>${rows}`;
        },
      },
      xAxis: {
        type: "category",
        inverse: true,
        data: buckets.map((b) => b.label),
        axisTick: { show: false },
        axisLine: { lineStyle: { color: c.grid } },
        axisLabel: { color: c.faint, fontSize: 11, fontFamily: CHART_FONT, hideOverlap: true },
      },
      yAxis: {
        type: "value",
        position: "right",
        minInterval: 1,
        splitNumber: 3,
        splitLine: { lineStyle: { color: c.grid, width: 1, type: "solid" } },
        axisLabel: { color: c.faint, fontSize: 11, fontFamily: CHART_FONT, formatter: (v: number) => formatNumber(v) },
      },
      series: SERIES.map((s, si) => ({
        name: s.name,
        type: "bar",
        stack: "events",
        barMaxWidth: 22,
        itemStyle: { color: c[s.color], borderColor: c.surface, borderWidth: 1 },
        emphasis: { focus: "series" },
        data: buckets.map((b, i) => ({
          value: b[s.key],
          itemStyle: { borderRadius: topIndex[i] === si ? [4, 4, 0, 0] : 0 },
        })),
      })),
    };
  }, [buckets, c]);

  const total = buckets.reduce((sum, b) => sum + b.allowed + b.blocked + b.info, 0);
  const summary = `فعالیت ${formatNumber(days)} روز اخیر: ${formatNumber(total)} رویداد`;
  return <EChart option={option} label={summary} className={className ?? "h-48"} />;
}
