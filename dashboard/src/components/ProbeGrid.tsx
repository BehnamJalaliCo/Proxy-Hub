import { useQueryClient } from "@tanstack/react-query";
import { Check, Minus, Radar, RefreshCw, X } from "lucide-react";
import { useState } from "react";

import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import * as api from "../lib/endpoints";
import { formatJalali, formatNumber, formatRelative } from "../lib/format";
import { PROBE_SOURCE, verdictInfo, type Tone } from "../lib/labels";
import { buildHeatmap, sourceStatus, verdictCounts, type HeatCell, type HeatColumn } from "../lib/probes";
import { keys } from "../lib/queries";
import { toast } from "../lib/toast";
import { TONE_SOFT } from "../lib/tones";
import type { ProbeSummary, ProbeVerdict } from "../lib/types";
import { Badge, StatusDot } from "./ui/Card";
import { EmptyState } from "./ui/Feedback";

const CELL_ICON: Record<Tone, typeof Check> = {
  success: Check,
  danger: X,
  warning: Minus,
  neutral: Minus,
  brand: Check,
  info: Minus,
};

const LEGEND: { tone: Tone; label: string }[] = [
  { tone: "success", label: "همه‌ی تلاش‌ها وصل شد" },
  { tone: "warning", label: "بعضی وصل نشد" },
  { tone: "danger", label: "هیچ‌کدام وصل نشد" },
];

function cellTitle(col: HeatColumn, cell: HeatCell): string {
  const asn = col.asn !== null ? ` (AS${String(col.asn)})` : "";
  return `${col.label}${asn}: ${formatNumber(cell.ok)} موفق، ${formatNumber(cell.fail)} ناموفق · ${formatRelative(cell.at)}`;
}

/** Node x Iranian network grid: each cell carries its numbers and an icon, not colour alone. */
export function ProbeGrid({ summary }: { summary: ProbeSummary }) {
  const qc = useQueryClient();
  const [running, setRunning] = useState<string | null>(null);
  const { columns, rows } = buildHeatmap(summary.targets);
  const counts = verdictCounts(summary.targets);

  const run = (nodeId: string, name: string) => {
    setRunning(nodeId);
    api
      .runProbe(nodeId)
      .then(() => {
        toast.success("سنجش در صف قرار گرفت", `«${name}» چند دقیقه‌ی دیگر دوباره از ایران سنجیده میشود.`);
        void qc.invalidateQueries({ queryKey: keys.probes });
      })
      .catch((e: unknown) => toast.error("سنجش شروع نشد", errorMessage(e)))
      .finally(() => setRunning(null));
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2 px-1">
        {summary.sources.length === 0 ? <span className="text-xs text-fg-faint">هنوز منبع سنجشی گزارش نداده</span> : null}
        {summary.sources.map((s) => {
          const status = sourceStatus(s);
          return (
            <span
              key={s.name}
              className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface-2 px-2.5 py-1 text-xs font-semibold text-fg"
              title={s.note ?? undefined}
            >
              <StatusDot tone={status.tone} />
              <span className="ltr">{PROBE_SOURCE[s.name] ?? s.name}</span>
              <span className="font-normal text-fg-muted">{status.text}</span>
              {status.showNote && s.note ? <span className="max-w-48 truncate font-normal text-fg-faint">· {s.note}</span> : null}
            </span>
          );
        })}
        <span className="ms-auto flex flex-wrap gap-1.5">
          {(Object.keys(counts) as ProbeVerdict[])
            .filter((v) => counts[v] > 0)
            .map((v) => (
              <Badge key={v} tone={verdictInfo(v).tone} dot>
                {verdictInfo(v).label}: {formatNumber(counts[v])}
              </Badge>
            ))}
        </span>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          icon={<Radar className="size-7" />}
          title="هنوز سنجشی نیست"
          description="وقتی node آماده‌ای باشد، پنل آن را از شبکه‌های ایران میسنجد و نتیجه اینجا به تفکیک اپراتور دیده میشود."
        />
      ) : (
        <>
          {/* The grid scrolls inside its own box on a phone; the page itself never scrolls sideways. */}
          <div className="overflow-x-auto rounded-2xl border border-line">
            <table className="w-full border-separate border-spacing-0 text-sm">
              <caption className="sr-only">باز بودن هر node از هر شبکه‌ی ایران: تعداد اتصال موفق از کل تلاش‌ها</caption>
              <thead>
                <tr>
                  <th scope="col" className="sticky start-0 z-10 bg-surface-2 px-3 py-2.5 text-start text-xs font-semibold text-fg-muted">
                    node
                  </th>
                  {columns.map((c) => (
                    <th key={c.key} scope="col" className="min-w-[5.5rem] bg-surface-2 px-2 py-2.5 text-center text-xs font-semibold text-fg">
                      <div className="truncate" title={c.network}>
                        {c.label}
                      </div>
                      {c.asn !== null ? <div className="ltr text-[0.68rem] font-normal text-fg-faint">AS{c.asn}</div> : null}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map(({ target, cells }) => {
                  const v = verdictInfo(target.verdict);
                  return (
                    <tr key={target.node_id}>
                      <th scope="row" className="sticky start-0 z-10 border-t border-line bg-surface px-3 py-2 text-start align-middle font-normal">
                        <div className="flex items-center gap-2">
                          <div className="min-w-0">
                            <div className="ltr max-w-[9rem] truncate text-right font-mono text-[0.8rem] font-bold text-fg" title={target.name}>
                              {target.name}
                            </div>
                            <Badge tone={v.tone} className="mt-1 px-2 py-0.5 text-[0.68rem]">
                              {v.label}
                            </Badge>
                          </div>
                          <button
                            type="button"
                            onClick={() => run(target.node_id, target.name)}
                            disabled={running !== null}
                            className="ms-auto grid size-8 shrink-0 place-items-center rounded-lg text-fg-faint hover:bg-surface-3 hover:text-brand disabled:opacity-50"
                            aria-label={`سنجش دوباره‌ی ${target.name}`}
                            title="سنجش دوباره"
                          >
                            <RefreshCw className={cn("size-4", running === target.node_id && "animate-spin")} />
                          </button>
                        </div>
                      </th>
                      {cells.map((cell, i) => {
                        const col = columns[i];
                        if (!col) return null;
                        if (!cell) {
                          return (
                            <td key={col.key} className="border-t border-line px-1.5 py-1.5 text-center text-xs text-fg-faint" title={`${col.label}: سنجش نشده`}>
                              —
                            </td>
                          );
                        }
                        const Icon = CELL_ICON[cell.tone];
                        return (
                          <td key={col.key} className="border-t border-line p-1.5">
                            <div
                              className={cn("flex h-11 items-center justify-center gap-1 rounded-xl text-xs font-bold", TONE_SOFT[cell.tone])}
                              title={cellTitle(col, cell)}
                            >
                              <Icon className="size-3.5" aria-hidden />
                              <span className="num">
                                {formatNumber(cell.ok)}/{formatNumber(cell.ok + cell.fail)}
                              </span>
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-1 text-xs text-fg-muted">
            {LEGEND.map((l) => {
              const Icon = CELL_ICON[l.tone];
              return (
                <span key={l.tone} className="flex items-center gap-1.5">
                  <span className={cn("grid size-5 place-items-center rounded-md", TONE_SOFT[l.tone])}>
                    <Icon className="size-3" />
                  </span>
                  {l.label}
                </span>
              );
            })}
            <span>عدد هر خانه: اتصال موفق از کل تلاش‌ها</span>
            {summary.last_run_at ? <span className="ms-auto">{formatJalali(summary.last_run_at, "datetime")}</span> : null}
          </div>
        </>
      )}
    </div>
  );
}
