// Chart colours resolved from the CSS tokens of the active theme (ECharts needs real colours).

import { useMemo } from "react";

import { useTheme } from "./theme";

export interface ChartColors {
  fg: string;
  muted: string;
  faint: string;
  surface: string;
  grid: string;
  brand: string;
  brand2: string;
  brandSoft: string;
  success: string;
  warning: string;
  danger: string;
  series1: string;
  series2: string;
  series3: string;
}

function cssVar(style: CSSStyleDeclaration, name: string): string {
  return style.getPropertyValue(name).trim();
}

/** Resolved colours of the current theme (re-read whenever the theme changes). */
export function useChartColors(): ChartColors {
  const { resolved } = useTheme();
  return useMemo(() => {
    const s = getComputedStyle(document.documentElement);
    return {
      fg: cssVar(s, "--ph-fg"),
      muted: cssVar(s, "--ph-fg-muted"),
      faint: cssVar(s, "--ph-fg-faint"),
      surface: cssVar(s, "--ph-surface"),
      grid: cssVar(s, "--ph-chart-grid"),
      brand: cssVar(s, "--ph-brand"),
      brand2: cssVar(s, "--ph-brand-2"),
      brandSoft: cssVar(s, "--ph-brand-soft"),
      success: cssVar(s, "--ph-success"),
      warning: cssVar(s, "--ph-warning"),
      danger: cssVar(s, "--ph-danger"),
      series1: cssVar(s, "--ph-series-1"),
      series2: cssVar(s, "--ph-series-2"),
      series3: cssVar(s, "--ph-series-3"),
    };
    // `resolved` is the trigger: CSS variables change with data-theme.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolved]);
}

export const CHART_FONT = '"Vazirmatn Variable", Vazirmatn, system-ui, sans-serif';
