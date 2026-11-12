// Thin React wrapper around Apache ECharts (tree-shaken: only the parts we draw). SVG renderer:
// crisp on phones, light on memory. Colours come from the CSS tokens of the active theme.

import { BarChart, GaugeChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { LegacyGridContainLabel } from "echarts/features";
import { SVGRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import { cn } from "../../lib/browser";

// LegacyGridContainLabel: every chart here lays its grid out with `grid.containLabel`. Without it
// ECharts 6 maps that to its new outer-bounds layout, which collapses the plot of a chart with an
// inverse (RTL) x axis to a thin strip, or to nothing, in the browser (members over 15 days).
echarts.use([BarChart, GaugeChart, GridComponent, TooltipComponent, SVGRenderer, LegacyGridContainLabel]);

export type EChartsOption = echarts.EChartsCoreOption;

export function EChart({
  option,
  className,
  label,
}: {
  option: EChartsOption;
  className?: string;
  /** Accessible name; the same data is always readable as text next to the chart. */
  label: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const instance = echarts.init(el, null, { renderer: "svg" });
    chart.current = instance;
    const ro = new ResizeObserver(() => instance.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  // dir=ltr: ECharts writes SVG text-anchor start/end meaning left/right, and the browser reads them
  // against the inherited direction (rtl here): y labels would run into the plot and end labels over
  // the line. Persian labels keep their order with rtlText (lib/chart.ts).
  return <div ref={ref} dir="ltr" role="img" aria-label={label} className={cn("w-full", className)} />;
}
