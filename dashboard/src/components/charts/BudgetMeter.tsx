import { useMemo } from "react";

import { CHART_FONT, useChartColors } from "../../lib/chart";
import { EChart, type EChartsOption } from "./EChart";

/** Arc meter of a ratio against a limit. The fill carries severity (brand -> warning -> danger);
 * the track is a lighter step of the same hue. Mirrored for RTL: it fills from the right. */
export function BudgetMeter({ ratio, label, className }: { ratio: number; label: string; className?: string }) {
  const c = useChartColors();
  const severity = ratio >= 1 ? c.danger : ratio >= 0.8 ? c.warning : c.brand;
  const option = useMemo<EChartsOption>(
    () => ({
      animationDuration: 1100,
      animationEasing: "cubicOut",
      textStyle: { fontFamily: CHART_FONT },
      series: [
        {
          type: "gauge",
          startAngle: -20,
          endAngle: 200,
          clockwise: false,
          min: 0,
          max: 1,
          radius: "100%",
          center: ["50%", "62%"],
          progress: { show: true, width: 16, roundCap: true, itemStyle: { color: severity } },
          axisLine: { roundCap: true, lineStyle: { width: 16, color: [[1, c.brandSoft]] } },
          pointer: { show: false },
          anchor: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          title: { show: false },
          detail: { show: false },
          data: [{ value: Math.max(0, Math.min(1, ratio)) }],
        },
      ],
    }),
    [ratio, severity, c.brandSoft],
  );
  return <EChart option={option} label={label} className={className ?? "h-44"} />;
}
