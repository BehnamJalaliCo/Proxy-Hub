// Charts of the «آمار» page (Apache ECharts, SVG). One axis per chart, thin marks with 4px rounded
// data ends, recessive hairline grid, an HTML legend for two or more series, and a tooltip on hover.
// Every chart has the same numbers as text next to it (sentences, cards or a table).

import { HeatmapChart, LineChart } from "echarts/charts";
import { MarkLineComponent, VisualMapComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { useMemo } from "react";

import type { DayPoint, ServerStat, TgGraph } from "../../lib/analytics";
import { dayLabel, formatBytes, formatPct, tgSeriesName, toTb, WEEK_ROWS } from "../../lib/analyticsText";
import { CHART_FONT, useChartColors, type ChartColors } from "../../lib/chart";
import { formatJalali, formatNumber } from "../../lib/format";
import { EChart, type EChartsOption } from "./EChart";

echarts.use([LineChart, HeatmapChart, MarkLineComponent, VisualMapComponent]);

type SeriesColor = "series1" | "series2" | "series3" | "brand";

function tooltip(c: ChartColors, trigger: "axis" | "item" = "axis") {
  return {
    trigger,
    axisPointer: trigger === "axis" ? { type: "shadow" as const, shadowStyle: { color: c.brandSoft } } : undefined,
    backgroundColor: c.surface,
    borderColor: c.grid,
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { color: c.fg, fontFamily: CHART_FONT, fontSize: 12 },
    extraCssText: "direction: rtl; text-align: right; border-radius: 12px; box-shadow: 0 12px 30px -12px rgba(0,0,0,.35);",
  };
}

function row(color: string, name: string, value: string): string {
  return `<div style="display:flex;align-items:center;gap:10px;justify-content:space-between;min-width:160px"><span style="display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:2px;background:${color}"></span>${name}</span><b>${value}</b></div>`;
}

const axisLabel = (c: ChartColors) => ({ color: c.faint, fontSize: 11, fontFamily: CHART_FONT });

function categoryX(c: ChartColors, labels: string[]) {
  return {
    type: "category" as const,
    inverse: true, // RTL: the newest is on the left, read from the right
    data: labels,
    axisTick: { show: false },
    axisLine: { lineStyle: { color: c.grid } },
    axisLabel: { ...axisLabel(c), hideOverlap: true },
  };
}

function valueY(c: ChartColors, format: (v: number) => string, extra: Record<string, unknown> = {}) {
  return {
    type: "value" as const,
    position: "right" as const,
    splitNumber: 3,
    splitLine: { lineStyle: { color: c.grid, width: 1, type: "solid" as const } },
    axisLabel: { ...axisLabel(c), formatter: format },
    ...extra,
  };
}

const base = { animationDuration: 700, animationEasing: "cubicOut" as const, textStyle: { fontFamily: CHART_FONT } };

/** Legend of named series (always shown for two or more series). */
export function ChartLegend({ items }: { items: { name: string; color: SeriesColor | "track" | "danger" | "warning" }[] }) {
  const cls: Record<string, string> = {
    series1: "bg-series-1",
    series2: "bg-series-2",
    series3: "bg-series-3",
    brand: "bg-brand",
    track: "bg-brand-soft ring-1 ring-line-strong",
    danger: "bg-danger",
    warning: "bg-warning",
  };
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label="راهنمای نمودار">
      {items.map((i) => (
        <li key={i.name} className="flex items-center gap-1.5">
          <span className={`size-2.5 rounded-sm ${cls[i.color] ?? ""}`} />
          {i.name}
        </li>
      ))}
    </ul>
  );
}

// --- traffic -------------------------------------------------------------------------------------------

/** TB served per Tehran day (outgoing: what users received). */
export function DailyTrafficChart({ days, className }: { days: DayPoint[]; className?: string }) {
  const c = useChartColors();
  const option = useMemo<EChartsOption>(
    () => ({
      ...base,
      grid: { top: 12, bottom: 4, left: 8, right: 8, containLabel: true },
      tooltip: {
        ...tooltip(c),
        formatter: (raw: unknown) => {
          const i = (Array.isArray(raw) ? (raw[0] as { dataIndex: number }) : (raw as { dataIndex: number })).dataIndex;
          const d = days[i];
          if (!d) return "";
          return `<div style="font-weight:700;margin-bottom:4px">${dayLabel(d.day)}</div>${row(c.series1, "خدمت به کاربران", d.out_bytes === null ? "بی داده" : formatBytes(d.out_bytes))}`;
        },
      },
      xAxis: categoryX(c, days.map((d) => dayLabel(d.day))),
      yAxis: valueY(c, (v: number) => formatNumber(v, v < 1 ? 2 : 1)),
      series: [
        {
          type: "bar",
          barMaxWidth: 22,
          itemStyle: { color: c.series1, borderRadius: [4, 4, 0, 0] },
          data: days.map((d) => (d.out_bytes === null ? "-" : toTb(d.out_bytes))),
        },
      ],
    }),
    [days, c],
  );
  const total = days.reduce((n, d) => n + (d.out_bytes ?? 0), 0);
  return <EChart option={option} label={`ترابایت خدمت به کاربران در هر روز؛ جمع ${formatBytes(total)}`} className={className ?? "h-56"} />;
}

/** Each server: used outgoing traffic of the billing period on a track as long as its included traffic. */
export function ServerTrafficChart({ servers, warnPct, className }: { servers: ServerStat[]; warnPct: number; className?: string }) {
  const c = useChartColors();
  const rows = useMemo(() => servers.filter((s) => s.included_bytes !== null && s.state !== "deleted"), [servers]);
  const option = useMemo<EChartsOption>(() => {
    const color = (s: ServerStat) => ((s.traffic_pct ?? 0) >= 100 ? c.danger : (s.traffic_pct ?? 0) >= warnPct ? c.warning : c.series1);
    return {
      ...base,
      grid: { top: 4, bottom: 4, left: 56, right: 8, containLabel: true },
      tooltip: {
        ...tooltip(c),
        formatter: (raw: unknown) => {
          const i = (Array.isArray(raw) ? (raw[0] as { dataIndex: number }) : (raw as { dataIndex: number })).dataIndex;
          const s = rows[i];
          if (!s) return "";
          return (
            `<div style="font-weight:700;margin-bottom:4px">${s.name} · ${s.location}</div>` +
            row(color(s), "مصرف این دوره", formatBytes(s.period_out_bytes)) +
            row(c.brandSoft, "ترافیک رایگان", formatBytes(s.included_bytes)) +
            row(c.faint, "درصد", formatPct(s.traffic_pct, 1))
          );
        },
      },
      xAxis: { type: "value", inverse: true, splitNumber: 3, splitLine: { lineStyle: { color: c.grid } }, axisLabel: { ...axisLabel(c), formatter: (v: number) => formatNumber(v) } },
      yAxis: {
        type: "category",
        position: "right",
        inverse: true,
        data: rows.map((s) => s.name),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { ...axisLabel(c), color: c.muted, fontSize: 12 },
      },
      series: [
        {
          type: "bar",
          barMaxWidth: 18,
          barGap: "-100%",
          silent: true,
          itemStyle: { color: c.brandSoft, borderRadius: [4, 0, 0, 4] },
          data: rows.map((s) => toTb(s.included_bytes)),
        },
        {
          type: "bar",
          barMaxWidth: 18,
          data: rows.map((s) => ({ value: toTb(s.period_out_bytes), itemStyle: { color: color(s), borderRadius: [4, 0, 0, 4] } })),
          label: {
            show: true,
            position: "left",
            color: c.muted,
            fontFamily: CHART_FONT,
            fontSize: 11,
            formatter: (p: { dataIndex: number }) => formatPct(rows[p.dataIndex]?.traffic_pct ?? null, 0),
          },
        },
      ],
    };
  }, [rows, c, warnPct]);
  return (
    // dir=ltr: SVG text anchors follow the page direction, and the category labels must sit outside the plot.
    <div dir="ltr" className={className} style={{ height: `${String(Math.max(96, rows.length * 40 + 24))}px` }}>
      <EChart option={option} label="مصرف ترافیک هر سرور در برابر ترافیک رایگان آن" className="h-full" />
    </div>
  );
}

/** Average CPU of all servers and of the busiest one, per hour, with the warning line. */
export function CpuChart({ points, threshold, className }: { points: { at: string; avg: number | null; max: number | null }[]; threshold: number; className?: string }) {
  const c = useChartColors();
  const option = useMemo<EChartsOption>(
    () => ({
      ...base,
      grid: { top: 16, bottom: 4, left: 8, right: 8, containLabel: true },
      tooltip: {
        ...tooltip(c),
        axisPointer: { type: "line", lineStyle: { color: c.grid } },
        formatter: (raw: unknown) => {
          const i = (Array.isArray(raw) ? (raw[0] as { dataIndex: number }) : (raw as { dataIndex: number })).dataIndex;
          const p = points[i];
          if (!p) return "";
          return `<div style="font-weight:700;margin-bottom:4px">${formatJalali(p.at, "short")}</div>${row(c.series1, "میانگین همه", formatPct(p.avg))}${row(c.series2, "پرکارترین سرور", formatPct(p.max))}`;
        },
      },
      xAxis: { ...categoryX(c, points.map((p) => formatJalali(p.at, "short"))), boundaryGap: false },
      yAxis: valueY(c, (v: number) => `${formatNumber(v)}٪`, { min: 0, max: 100 }),
      series: [
        {
          type: "line",
          name: "avg",
          showSymbol: false,
          lineStyle: { width: 2, color: c.series1 },
          itemStyle: { color: c.series1 },
          data: points.map((p) => p.avg ?? "-"),
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: c.warning, width: 1, type: "dashed" },
            label: { formatter: `آستانه ${formatPct(threshold)}`, color: c.muted, fontFamily: CHART_FONT, fontSize: 11, position: "insideEndTop" },
            data: [{ yAxis: threshold }],
          },
        },
        { type: "line", name: "max", showSymbol: false, lineStyle: { width: 2, color: c.series2 }, itemStyle: { color: c.series2 }, data: points.map((p) => p.max ?? "-") },
      ],
    }),
    [points, c, threshold],
  );
  return <EChart option={option} label="CPU سرورها در هر ساعت" className={className ?? "h-52"} />;
}

// --- channel -------------------------------------------------------------------------------------------

/** Members of the main channel over time (one series: no legend). */
export function MembersChart({ points, className }: { points: { at: string; count: number }[]; className?: string }) {
  const c = useChartColors();
  const option = useMemo<EChartsOption>(() => {
    const values = points.map((p) => p.count);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const pad = Math.max(1, Math.round((max - min) * 0.15));
    return {
      ...base,
      grid: { top: 12, bottom: 4, left: 8, right: 8, containLabel: true },
      tooltip: {
        ...tooltip(c),
        axisPointer: { type: "line", lineStyle: { color: c.grid } },
        formatter: (raw: unknown) => {
          const i = (Array.isArray(raw) ? (raw[0] as { dataIndex: number }) : (raw as { dataIndex: number })).dataIndex;
          const p = points[i];
          return p ? `<div style="font-weight:700;margin-bottom:4px">${formatJalali(p.at, "short")}</div>${row(c.series1, "عضو", formatNumber(p.count))}` : "";
        },
      },
      xAxis: { ...categoryX(c, points.map((p) => formatJalali(p.at, "day"))), boundaryGap: false },
      yAxis: valueY(c, (v: number) => formatNumber(v), { min: Math.max(0, min - pad), max: max + pad, scale: true }),
      series: [
        {
          type: "line",
          showSymbol: points.length < 3,
          symbolSize: 8,
          lineStyle: { width: 2, color: c.series1 },
          itemStyle: { color: c.series1, borderColor: c.surface, borderWidth: 2 },
          areaStyle: { color: c.series1, opacity: 0.1 },
          data: values,
          endLabel: { show: true, color: c.fg, fontFamily: CHART_FONT, fontWeight: "bold", formatter: (p: { value: number }) => formatNumber(p.value) },
        },
      ],
    };
  }, [points, c]);
  return <EChart option={option} label="تعداد اعضای کانال اصلی در طول زمان" className={className ?? "h-52"} />;
}

/** Joins (up) and leaves (down) per Tehran day: above/below the zero line. */
export function JoinsLeavesChart({ days, className }: { days: DayPoint[]; className?: string }) {
  const c = useChartColors();
  const option = useMemo<EChartsOption>(
    () => ({
      ...base,
      grid: { top: 12, bottom: 4, left: 8, right: 8, containLabel: true },
      tooltip: {
        ...tooltip(c),
        formatter: (raw: unknown) => {
          const i = (Array.isArray(raw) ? (raw[0] as { dataIndex: number }) : (raw as { dataIndex: number })).dataIndex;
          const d = days[i];
          if (!d) return "";
          const j = d.joins ?? 0;
          const l = d.leaves ?? 0;
          return `<div style="font-weight:700;margin-bottom:4px">${dayLabel(d.day)}</div>${row(c.series1, "ورود", formatNumber(j))}${row(c.series2, "خروج", formatNumber(l))}${row(c.faint, "خالص", formatNumber(j - l))}`;
        },
      },
      xAxis: { ...categoryX(c, days.map((d) => dayLabel(d.day))), axisLine: { onZero: true, lineStyle: { color: c.grid } } },
      yAxis: valueY(c, (v: number) => formatNumber(Math.abs(v)), { minInterval: 1 }),
      series: [
        { type: "bar", stack: "moves", barMaxWidth: 18, itemStyle: { color: c.series1, borderRadius: [4, 4, 0, 0] }, data: days.map((d) => d.joins ?? 0) },
        { type: "bar", stack: "moves", barMaxWidth: 18, itemStyle: { color: c.series2, borderRadius: [0, 0, 4, 4] }, data: days.map((d) => -(d.leaves ?? 0)) },
      ],
    }),
    [days, c],
  );
  return <EChart option={option} label="ورود و خروج روزانه‌ی اعضای کانال اصلی" className={className ?? "h-52"} />;
}

/** Ranked horizontal bars, one hue, largest first (sources, countries, provinces, networks). */
export function RankedBars({ items, format, label, className }: { items: { label: string; value: number }[]; format: (v: number) => string; label: string; className?: string }) {
  const c = useChartColors();
  const option = useMemo<EChartsOption>(
    () => ({
      ...base,
      grid: { top: 4, bottom: 4, left: 64, right: 8, containLabel: true },
      tooltip: {
        ...tooltip(c, "item"),
        formatter: (p: { dataIndex: number }) => {
          const it = items[p.dataIndex];
          return it ? row(c.series1, it.label, format(it.value)) : "";
        },
      },
      xAxis: { type: "value", inverse: true, show: false },
      yAxis: {
        type: "category",
        position: "right",
        inverse: true,
        data: items.map((i) => i.label),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { ...axisLabel(c), color: c.muted, fontSize: 12, width: 130, overflow: "truncate" },
      },
      series: [
        {
          type: "bar",
          barMaxWidth: 16,
          showBackground: true,
          backgroundStyle: { color: c.brandSoft, borderRadius: 4 },
          itemStyle: { color: c.series1, borderRadius: [4, 0, 0, 4] },
          label: { show: true, position: "left", color: c.muted, fontFamily: CHART_FONT, fontSize: 11, formatter: (p: { value: number }) => format(p.value) },
          data: items.map((i) => i.value),
        },
      ],
    }),
    [items, c, format],
  );
  return (
    <div dir="ltr" className={className} style={{ height: `${String(Math.max(72, items.length * 34 + 16))}px` }}>
      <EChart option={option} label={label} className="h-full" />
    </div>
  );
}

/** Traffic by weekday and hour (Tehran), one hue light -> dark. */
export function UsageHeatmap({ cells, className }: { cells: { dow: number; hour: number; bytes: number; conns: number }[]; className?: string }) {
  const c = useChartColors();
  const option = useMemo<EChartsOption>(() => {
    const byKey = new Map(cells.map((x) => [`${String(x.dow)}-${String(x.hour)}`, x]));
    const data: [number, number, number][] = [];
    WEEK_ROWS.forEach((w, y) => {
      for (let h = 0; h < 24; h += 1) data.push([h, y, byKey.get(`${String(w.dow)}-${String(h)}`)?.bytes ?? 0]);
    });
    const max = Math.max(1, ...data.map((d) => d[2]));
    return {
      ...base,
      grid: { top: 4, bottom: 4, left: 4, right: 4, containLabel: true },
      tooltip: {
        ...tooltip(c, "item"),
        formatter: (p: { value: [number, number, number] }) => {
          const [h, y, v] = p.value;
          const w = WEEK_ROWS[y];
          const cell = w ? byKey.get(`${String(w.dow)}-${String(h)}`) : undefined;
          return `<div style="font-weight:700;margin-bottom:4px">${w?.label ?? ""}، ساعت ${formatNumber(h)} تا ${formatNumber(h + 1)}</div>${row(c.brand, "ترافیک", formatBytes(v))}${row(c.faint, "بیشترین اتصال هم‌زمان", formatNumber(cell?.conns ?? 0))}`;
        },
      },
      xAxis: {
        type: "category",
        inverse: true,
        data: Array.from({ length: 24 }, (_, h) => formatNumber(h)),
        splitArea: { show: false },
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { ...axisLabel(c), interval: 2 },
      },
      yAxis: {
        type: "category",
        position: "right",
        data: WEEK_ROWS.map((w) => w.label),
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { ...axisLabel(c), color: c.muted },
      },
      visualMap: { show: false, min: 0, max, inRange: { color: [c.brandSoft, c.brand] } },
      series: [{ type: "heatmap", data, itemStyle: { borderColor: c.surface, borderWidth: 2, borderRadius: 3 }, emphasis: { itemStyle: { borderColor: c.fg } } }],
    };
  }, [cells, c]);
  return (
    <div dir="ltr">
      <EChart option={option} label="ترافیک به تفکیک روز هفته و ساعت (وقت تهران)" className={className ?? "h-64"} />
    </div>
  );
}

// --- Telegram's own graphs (operator account, D38) --------------------------------------------------------

const TG_COLORS: SeriesColor[] = ["series1", "series2", "series3", "brand"];

/** One of Telegram's channel graphs: at most four series (the rest are left out of the chart). */
export function TelegramGraph({ graph, label, className }: { graph: TgGraph; label: string; className?: string }) {
  const c = useChartColors();
  const series = graph.series.slice(0, 4);
  const option = useMemo<EChartsOption>(
    () => ({
      ...base,
      grid: { top: 12, bottom: 4, left: 8, right: 8, containLabel: true },
      tooltip: { ...tooltip(c), axisPointer: { type: "line", lineStyle: { color: c.grid } } },
      xAxis: { ...categoryX(c, graph.x.map((ms) => formatJalali(new Date(ms), "day"))), boundaryGap: false },
      yAxis: valueY(c, (v: number) => formatNumber(v), { scale: true }),
      series: series.map((s, i) => ({
        type: "line",
        name: tgSeriesName(s.name),
        showSymbol: false,
        lineStyle: { width: 2, color: c[TG_COLORS[i] ?? "series1"] },
        itemStyle: { color: c[TG_COLORS[i] ?? "series1"] },
        data: s.values,
      })),
    }),
    [graph, series, c],
  );
  return (
    <div className="flex flex-col gap-2">
      {series.length > 1 ? <ChartLegend items={series.map((s, i) => ({ name: tgSeriesName(s.name), color: TG_COLORS[i] ?? "series1" }))} /> : null}
      <EChart option={option} label={label} className={className ?? "h-52"} />
    </div>
  );
}
