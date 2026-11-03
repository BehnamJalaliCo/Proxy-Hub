import { ListFilter, ScrollText, Search, X } from "lucide-react";
import { useMemo, useState } from "react";

import { ActivityChart, ActivityLegend } from "../components/charts/ActivityChart";
import { EventRow } from "../components/EventRow";
import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { EmptyState, ErrorState, Skeleton, SkeletonLines } from "../components/ui/Feedback";
import { Segmented } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { EMPTY_FILTER, filterEvents, verdictOf, type EventFilter } from "../lib/events";
import { formatNumber } from "../lib/format";
import { actionLabel, actorLabel } from "../lib/labels";
import { useEventPages } from "../lib/queries";
import type { AuditEvent } from "../lib/types";

const labelOf = (e: AuditEvent) => `${actionLabel(e.action)} ${actorLabel(e.actor)}`;

export default function EventsPage() {
  const pages = useEventPages();
  const [filter, setFilter] = useState<EventFilter>(EMPTY_FILTER);
  const all = useMemo(() => pages.data?.pages.flat() ?? [], [pages.data]);
  const shown = useMemo(() => filterEvents(all, filter, labelOf), [all, filter]);
  const actors = useMemo(() => Array.from(new Set(all.map((e) => e.actor))).sort(), [all]);
  const active = filter.text !== "" || filter.verdict !== "all" || filter.mode !== "all" || filter.actor !== "";
  const counts = useMemo(() => {
    const c = { allowed: 0, blocked: 0, info: 0, dry: 0 };
    for (const e of shown) {
      c[verdictOf(e)] += 1;
      if (e.dry_run) c.dry += 1;
    }
    return c;
  }, [shown]);

  const set = <K extends keyof EventFilter>(k: K, v: EventFilter[K]) => setFilter((f) => ({ ...f, [k]: v }));

  return (
    <>
      <PageHeader title="رویدادها" subtitle="هر کاری که پنل، چرخه‌ها و مالک کرده‌اند، با دلیل (actions_log). هیچ IP کاربری اینجا نیست." />

      {/* One filter row above everything it scopes (chart and list). */}
      <Card className="mb-4 p-3 sm:p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute inset-y-0 start-3 my-auto size-4 text-fg-faint" />
            <input
              type="search"
              value={filter.text}
              onChange={(e) => set("text", e.target.value)}
              placeholder="جست‌وجو در کار، هدف، دلیل یا نتیجه…"
              aria-label="جست‌وجو در رویدادها"
              className="h-11 w-full rounded-xl border border-line bg-surface-2 ps-9 pe-3 text-sm text-fg outline-none placeholder:text-fg-faint focus:border-brand focus:ring-4 focus:ring-[var(--ph-ring)]"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Segmented
              label="نتیجه‌ی قفل ایمنی"
              value={filter.verdict}
              onChange={(v) => set("verdict", v)}
              options={[
                { value: "all", label: "همه" },
                { value: "allowed", label: "مجاز" },
                { value: "blocked", label: "ردشده" },
                { value: "info", label: "گزارشی" },
              ]}
            />
            <Segmented
              label="حالت"
              value={filter.mode}
              onChange={(v) => set("mode", v)}
              options={[
                { value: "all", label: "همه" },
                { value: "dry", label: "خشک" },
                { value: "real", label: "واقعی" },
              ]}
            />
            <label className="relative">
              <span className="sr-only">انجام‌دهنده</span>
              <select
                value={filter.actor}
                onChange={(e) => set("actor", e.target.value)}
                className="h-10 appearance-none rounded-xl border border-line bg-surface-2 ps-3 pe-8 text-[0.82rem] font-semibold text-fg outline-none focus:border-brand"
              >
                <option value="">همه‌ی انجام‌دهنده‌ها</option>
                {actors.map((a) => (
                  <option key={a} value={a}>
                    {actorLabel(a)}
                  </option>
                ))}
              </select>
              <ListFilter className="pointer-events-none absolute inset-y-0 end-2.5 my-auto size-4 text-fg-faint" />
            </label>
            {active ? (
              <Button variant="ghost" size="sm" icon={<X className="size-4" />} onClick={() => setFilter(EMPTY_FILTER)}>
                پاک کردن
              </Button>
            ) : null}
          </div>
        </div>
      </Card>

      {pages.isPending ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-64 rounded-3xl" />
          <Card className="p-5">
            <SkeletonLines lines={6} />
          </Card>
        </div>
      ) : pages.isError ? (
        <Card>
          <ErrorState error={pages.error} onRetry={() => void pages.refetch()} />
        </Card>
      ) : all.length === 0 ? (
        <Card>
          <EmptyState icon={<ScrollText className="size-7" />} title="هنوز رویدادی ثبت نشده" description="با اولین کار پنل یا چرخه‌ها، رویدادها اینجا با دلیل نشان داده میشوند." />
        </Card>
      ) : (
        <div className="flex flex-col gap-4 sm:gap-5">
          <Card>
            <CardHeader
              title="فعالیت ۱۴ روز اخیر"
              subtitle={`بر پایه‌ی ${formatNumber(shown.length)} رویداد ${active ? "پالایش‌شده" : "بارگذاری‌شده"}`}
              action={<ActivityLegend />}
              className="flex-col sm:flex-row"
            />
            <div className="p-4 sm:p-5">
              <ActivityChart events={shown} days={14} className="h-60" />
            </div>
          </Card>

          <Card>
            <CardHeader
              icon={<ScrollText className="size-5" />}
              title="فهرست رویدادها"
              subtitle={`${formatNumber(shown.length)} از ${formatNumber(all.length)} رویداد بارگذاری‌شده`}
              action={
                <div className="hidden flex-wrap gap-1.5 sm:flex">
                  <Badge tone="info">مجاز {formatNumber(counts.allowed)}</Badge>
                  <Badge tone="danger">ردشده {formatNumber(counts.blocked)}</Badge>
                  <Badge tone="warning">خشک {formatNumber(counts.dry)}</Badge>
                </div>
              }
            />
            <div className="p-2 sm:p-3">
              {shown.length === 0 ? (
                <EmptyState
                  icon={<ListFilter className="size-7" />}
                  title="رویدادی با این پالایه نیست"
                  action={
                    <Button variant="secondary" size="sm" onClick={() => setFilter(EMPTY_FILTER)}>
                      پاک کردن پالایه
                    </Button>
                  }
                />
              ) : (
                <ul className="flex flex-col divide-y divide-line">
                  {shown.map((e) => (
                    <EventRow key={e.id} event={e} />
                  ))}
                </ul>
              )}
              <div className="flex justify-center p-3">
                {pages.hasNextPage ? (
                  <Button variant="secondary" loading={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}>
                    بارگذاری رویدادهای قدیمی‌تر
                  </Button>
                ) : (
                  <span className="text-xs text-fg-faint">به آغاز فهرست رسیدید.</span>
                )}
              </div>
            </div>
          </Card>
        </div>
      )}
    </>
  );
}
