// «آمار»: how much the proxies serve, what it costs, how close each server is to its limits, who uses
// the proxies (aggregate counters only), how the channel grows and where new members come from.
// Every number comes from the panel's own tables; nothing is invented while data is missing.

import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  Check,
  Coins,
  Eye,
  Gauge,
  Globe2,
  Link2,
  MapPinned,
  Megaphone,
  Pencil,
  Plus,
  Radio,
  Save,
  Server,
  Share2,
  Sparkles,
  TriangleAlert,
  UsersRound,
  Wifi,
  X,
} from "lucide-react";
import { motion } from "motion/react";
import { useId, useState, type ReactNode, type SubmitEvent } from "react";

import {
  ChartLegend,
  CpuChart,
  DailyTrafficChart,
  JoinsLeavesChart,
  MembersChart,
  RankedBars,
  ServerTrafficChart,
  TelegramGraph,
  UsageHeatmap,
} from "../components/charts/AnalyticsCharts";
import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { CopyButton, EmptyState, LoadError, Skeleton, SkeletonLines } from "../components/ui/Feedback";
import { Field, Segmented, TextInput, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import {
  analyticsKeys,
  createTrackingLink,
  putAnalyticsSettings,
  renameTrackingLink,
  useAnalyticsSeries,
  useAnalyticsSettings,
  useAnalyticsSummary,
  useAnalyticsUsage,
  useChannelStats,
  useTrackingLinks,
  type AnalyticsSeries,
  type AnalyticsSummary,
  type AnalyticsUsage,
  type ChannelStatsAnswer,
  type Thresholds,
  type TrackingLink,
} from "../lib/analytics";
import {
  busiestSentence,
  channelStatsOutcomeText,
  channelStatsStatusText,
  costSentence,
  countryName,
  dayLabel,
  formatBps,
  formatBytes,
  formatMb,
  formatPct,
  formatSigned,
  ispName,
  membersSentence,
  postsSentence,
  regionName,
  SIGNAL_TEXT,
  sourceLabel,
  sourcesSentence,
  trafficSentence,
  usersSentence,
} from "../lib/analyticsText";
import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import { formatDecimal, formatJalali, formatNumber, formatRelative } from "../lib/format";
import type { Tone } from "../lib/labels";
import { toast } from "../lib/toast";
import { TONE_SOFT } from "../lib/tones";

type Days = "7" | "30" | "90";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

// --- small building blocks -------------------------------------------------------------------------

function Stat({ icon, label, value, sub, tone = "brand", index = 0 }: { icon: ReactNode; label: string; value: ReactNode; sub?: ReactNode; tone?: Tone; index?: number }) {
  return (
    <motion.div {...fadeUp(index)}>
      <Card className="h-full p-4 sm:p-5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.8rem] font-semibold text-fg-muted">{label}</span>
          <span className={cn("grid size-8 place-items-center rounded-xl", TONE_SOFT[tone])}>{icon}</span>
        </div>
        <div className="mt-3 text-[1.45rem] leading-tight font-extrabold tracking-tight text-fg sm:text-[1.7rem]">{value}</div>
        {sub ? <div className="mt-1.5 text-xs leading-6 text-fg-muted">{sub}</div> : null}
      </Card>
    </motion.div>
  );
}

function Section({ id, icon, title, sentence, children }: { id: string; icon: ReactNode; title: string; sentence?: string | null; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-4">
      <div className="flex items-start gap-3 pt-2">
        <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))] text-white shadow-[0_8px_22px_-12px_var(--ph-brand)]">
          {icon}
        </span>
        <div className="min-w-0">
          <h2 id={id} className="text-lg leading-8 font-extrabold text-fg">
            {title}
          </h2>
          {sentence ? <p className="text-sm leading-7 text-fg-muted">{sentence}</p> : null}
        </div>
      </div>
      {children}
    </section>
  );
}

function NoData({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return <EmptyState icon={icon} title={title} description={children} className="py-8" />;
}

function ChartCard({ icon, title, sentence, children, className, legend }: { icon: ReactNode; title: string; sentence?: string | null; children: ReactNode; className?: string; legend?: ReactNode }) {
  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader icon={icon} title={title} subtitle={sentence ?? undefined} />
      <div className="flex flex-col gap-3 px-4 pt-3 pb-4 sm:px-5">
        {legend}
        {children}
      </div>
    </Card>
  );
}

function TableDetails({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <details className="group rounded-2xl border border-line bg-surface-2/60 text-sm">
      <summary className="cursor-pointer list-none px-3 py-2 font-semibold text-fg-muted select-none hover:text-fg">{summary}</summary>
      <div className="overflow-x-auto px-3 pb-3">{children}</div>
    </details>
  );
}

const th = "px-2 py-1.5 text-start text-xs font-semibold text-fg-faint whitespace-nowrap";
const td = "px-2 py-1.5 whitespace-nowrap tabular-nums";

// --- the headline -----------------------------------------------------------------------------------------

function Headline({ s, usage, stats }: { s: AnalyticsSummary; usage: AnalyticsUsage | undefined; stats: ChannelStatsAnswer | undefined }) {
  const lines: { icon: ReactNode; text: string }[] = [
    { icon: <Wifi className="size-4" />, text: trafficSentence(s) },
    { icon: <Coins className="size-4" />, text: costSentence(s) },
    { icon: <UsersRound className="size-4" />, text: membersSentence(s.channel.main) },
  ];
  const source = sourcesSentence(s.top_sources);
  if (source) lines.push({ icon: <Link2 className="size-4" />, text: source });
  if (usage?.has_data) lines.push({ icon: <Globe2 className="size-4" />, text: usersSentence(usage) });
  const posts = postsSentence(stats?.main ?? null);
  if (posts) lines.push({ icon: <Eye className="size-4" />, text: posts });
  return (
    <Card className="ph-glow-border relative overflow-clip">
      <div aria-hidden className="pointer-events-none absolute -top-24 -left-24 size-72 rounded-full bg-[radial-gradient(circle,var(--ph-bg-glow-1),transparent_70%)]" />
      <div className="relative flex flex-col gap-3 p-5 sm:p-6">
        <div className="flex items-center gap-2 text-sm font-bold text-brand">
          <Sparkles className="size-4" />
          خلاصه به زبان ساده
        </div>
        <ul className="flex flex-col gap-2.5">
          {lines.map((l, i) => (
            <motion.li key={l.text} {...fadeUp(i)} className="flex items-start gap-2.5 text-[0.98rem] leading-8 text-fg">
              <span className="mt-1.5 grid size-6 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand">{l.icon}</span>
              <span>{l.text}</span>
            </motion.li>
          ))}
        </ul>
        <p className="text-xs leading-6 text-fg-faint">
          همه‌ی این عددها آمار کلی‌اند: هیچ IP یا رفتار یک کاربر پروکسی ذخیره نمیشود. «ماه» همان ماه میلادی صورت‌حساب هتزنر است؛ یک ترابایت = ۱۰۲۴ گیگابایت (حساب هتزنر).
        </p>
      </div>
    </Card>
  );
}

function Warnings({ s }: { s: AnalyticsSummary }) {
  if (s.warnings.length === 0) return null;
  return (
    <div role="alert" className="flex flex-col gap-2 rounded-3xl border border-warning/30 bg-warning-soft p-4">
      {s.warnings.map((w) => (
        <p key={`${w.kind}-${w.node_id}`} className="flex items-start gap-2 text-sm leading-7 text-fg">
          <TriangleAlert className="mt-1 size-4 shrink-0 text-warning" />
          <span>
            {w.kind === "traffic" ? (
              <>
                سرور <b>{w.name}</b> به {formatPct(w.value)} ترافیک رایگان این ماهش رسیده است (آستانه {formatPct(w.threshold)}). بیشتر از سقف، هر
                ترابایت هزینه دارد.
              </>
            ) : (
              <>
                میانگین CPU سرور <b>{w.name}</b> در {formatNumber(s.thresholds.cpu_window_min)} دقیقه‌ی گذشته {formatPct(w.value)} بوده است (آستانه{" "}
                {formatPct(w.threshold)}).
              </>
            )}
          </span>
        </p>
      ))}
    </div>
  );
}

// --- traffic and cost ---------------------------------------------------------------------------------------

function TrafficSection({ s, series }: { s: AnalyticsSummary; series: AnalyticsSeries | undefined }) {
  const t = s.traffic;
  const live = s.servers.filter((x) => x.state !== "deleted");
  const nodesStatus = s.collector.nodes;
  const days = series?.days ?? [];
  const daysWithData = days.filter((d) => d.out_bytes !== null);
  const best = daysWithData.reduce<(typeof days)[number] | null>((a, d) => (a === null || (d.out_bytes ?? 0) > (a.out_bytes ?? 0) ? d : a), null);
  return (
    <Section id="stats-traffic" icon={<Wifi className="size-5" />} title="ترافیک و هزینه" sentence={t.sampling_since === null ? null : trafficSentence(s)}>
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat index={0} icon={<Wifi className="size-4" />} label="خدمت این ماه" value={t.sampling_since === null ? "—" : formatBytes(t.month_out_bytes)} sub={t.sampling_since === null ? "هنوز سنجیده نشده" : `امروز: ${formatBytes(t.today_out_bytes)}`} />
        <Stat
          index={1}
          icon={<Activity className="size-4" />}
          label="میانگین روزانه"
          tone="info"
          value={t.avg_day_out_bytes === null ? "—" : formatBytes(t.avg_day_out_bytes)}
          sub={t.last_month_out_bytes > 0 ? `ماه پیش: ${formatBytes(t.last_month_out_bytes)}` : "ماه پیش داده‌ای نیست"}
        />
        <Stat
          index={2}
          icon={<Coins className="size-4" />}
          label="هزینه‌ی این ماه تا امروز"
          tone="warning"
          value={`${formatDecimal(s.cost.total_month_to_date_eur)} €`}
          sub={`پیش‌بینی کل ماه: ${formatDecimal(s.cost.projected_month_eur)} €`}
        />
        <Stat
          index={3}
          icon={<BarChart3 className="size-4" />}
          label="هزینه‌ی هر ترابایت"
          tone="success"
          value={s.cost.per_tb_eur === null ? "—" : `${formatDecimal(s.cost.per_tb_eur)} €`}
          sub={s.cost.traffic_overage_eur === "0.00" ? "بی هزینه‌ی ترافیک اضافه" : `ترافیک اضافه: ${formatDecimal(s.cost.traffic_overage_eur)} €`}
        />
      </div>
      {t.sampling_since === null ? (
        <Card>
          <NoData icon={<Server className="size-7" />} title="هنوز سروری سنجیده نشده">
            وقتی اولین node ساخته شود، ترافیک و CPU آن هر ۱۵ دقیقه از خود هتزنر خوانده میشود (فقط خواندن).
            {nodesStatus?.at ? ` آخرین نوبت: ${formatRelative(nodesStatus.at)}.` : ""}
          </NoData>
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-12">
          <ChartCard
            className="lg:col-span-7"
            icon={<BarChart3 className="size-5" />}
            title="ترابایت خدمت در هر روز"
            sentence={best ? `پرترافیک‌ترین روز: ${dayLabel(best.day)} با ${formatBytes(best.out_bytes)}.` : "در این بازه هنوز ترافیکی ثبت نشده است."}
          >
            <DailyTrafficChart days={days} />
            <TableDetails summary="نمایش جدول روزها">
              <table className="w-full">
                <thead>
                  <tr>
                    <th className={th}>روز</th>
                    <th className={th}>خدمت به کاربران</th>
                    <th className={th}>دریافتی سرورها</th>
                  </tr>
                </thead>
                <tbody>
                  {[...daysWithData].reverse().map((d) => (
                    <tr key={d.day} className="border-t border-line">
                      <td className={td}>{dayLabel(d.day)}</td>
                      <td className={td}>{formatBytes(d.out_bytes)}</td>
                      <td className={td}>{formatBytes(d.in_bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableDetails>
          </ChartCard>
          <ChartCard
            className="lg:col-span-5"
            icon={<Server className="size-5" />}
            title="هر سرور در برابر سقف رایگانش"
            sentence={busiestSentence(s) ?? "هنوز سروری با داده‌ی ترافیک نیست."}
            legend={
              <ChartLegend
                items={[
                  { name: "مصرف این دوره", color: "series1" },
                  { name: "ترافیک رایگان", color: "track" },
                  { name: `بالای ${formatPct(s.thresholds.traffic_warn_pct)}`, color: "warning" },
                ]}
              />
            }
          >
            {live.some((x) => x.included_bytes !== null) ? <ServerTrafficChart servers={live} warnPct={s.thresholds.traffic_warn_pct} /> : null}
            <ServersTable s={s} />
          </ChartCard>
          {series && months(series).length > 0 ? (
            <Card className="lg:col-span-12">
              <CardHeader icon={<BarChart3 className="size-5" />} title="ترافیک هر ماه" subtitle="ماه‌های میلادی صورت‌حساب هتزنر؛ تاریخ‌ها شمسی." />
              <ul className="grid grid-cols-2 gap-2 p-5 sm:grid-cols-3 lg:grid-cols-6">
                {months(series).map((m) => (
                  <li key={m.start} className="rounded-2xl border border-line bg-surface-2/60 p-3">
                    <div className="text-xs text-fg-muted">
                      {formatJalali(m.start, "day")} تا {formatJalali(new Date(new Date(m.end).getTime() - 86_400_000), "day")}
                    </div>
                    <div className="mt-1 text-base font-extrabold text-fg">{formatBytes(m.out_bytes)}</div>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
      )}
    </Section>
  );
}

/** Months from the first one with traffic (older months only say "no data"). */
function months(series: AnalyticsSeries) {
  const first = series.months.findIndex((m) => m.out_bytes > 0);
  return first < 0 ? [] : series.months.slice(first);
}

function ServersTable({ s }: { s: AnalyticsSummary }) {
  if (s.servers.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-2xl border border-line">
      <table className="w-full text-sm">
        <thead className="bg-surface-2/70">
          <tr>
            <th className={th}>سرور</th>
            <th className={th}>این ماه</th>
            <th className={th}>از سقف</th>
            <th className={th}>CPU</th>
            <th className={th}>هزینه</th>
          </tr>
        </thead>
        <tbody>
          {s.servers.map((x) => (
            <tr key={x.node_id} className="border-t border-line">
              <td className={cn(td, "font-semibold")}>
                <span className="block">{x.name}</span>
                <span className="block text-xs font-normal text-fg-faint">{x.state === "deleted" ? "حذف‌شده" : x.location}</span>
              </td>
              <td className={td}>{formatBytes(x.month_out_bytes)}</td>
              <td className={td}>{formatPct(x.traffic_pct, 1)}</td>
              <td className={td}>{x.cpu_avg === null ? "—" : formatPct(x.cpu_avg)}</td>
              <td className={td}>{x.cost_month_eur === null ? "—" : `${formatDecimal(x.cost_month_eur)} €`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- proxy users (D39) ----------------------------------------------------------------------------------------

function UsersSection({ usage, isError, error, refetch }: { usage: AnalyticsUsage | undefined; isError: boolean; error: unknown; refetch: () => void }) {
  if (isError) {
    return (
      <Section id="stats-users" icon={<Globe2 className="size-5" />} title="کاربران">
        <Card>
          <LoadError error={error} onRetry={refetch} compact />
        </Card>
      </Section>
    );
  }
  if (!usage) {
    return (
      <Section id="stats-users" icon={<Globe2 className="size-5" />} title="کاربران">
        <Skeleton className="h-40 rounded-3xl" />
      </Section>
    );
  }
  if (!usage.has_data) {
    return (
      <Section id="stats-users" icon={<Globe2 className="size-5" />} title="کاربران">
        <Card>
          <NoData icon={<Globe2 className="size-7" />} title="هنوز داده‌ای نیست">
            کشور، استان، اپراتور (همراه اول، ایرانسل، مخابرات، شاتل و …)، ساعت‌های اوج و مصرف هر کاربر را عامل خود هر سرور میسازد: IP فقط در حافظه‌ی همان
            سرور به عدد کلی تبدیل میشود و هرگز ذخیره یا فرستاده نمیشود؛ هر دسته‌ی کمتر از {formatNumber(usage.min_users)} نفر در «بقیه» میرود. این بخش با
            اولین گزارش عامل پر میشود.
          </NoData>
        </Card>
      </Section>
    );
  }
  const last = usage.days.at(-1);
  const p = usage.per_user_mb;
  const asBars = (items: AnalyticsUsage["by_country"], name: (k: string) => string) =>
    items.slice(0, 10).map((b) => ({ label: name(b.key), value: b.avg_daily_users }));
  const peak = usage.heatmap.reduce<(typeof usage.heatmap)[number] | null>((a, c) => (a === null || c.bytes > a.bytes ? c : a), null);
  return (
    <Section id="stats-users" icon={<Globe2 className="size-5" />} title="کاربران" sentence={usersSentence(usage)}>
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat index={0} icon={<UsersRound className="size-4" />} label="کاربر یکتای آخرین روز" value={last ? formatNumber(last.unique_users) : "—"} sub="جمع سرورها؛ هر سرور جدا میشمارد" />
        <Stat index={1} icon={<Gauge className="size-4" />} label="نصف کاربران کمتر از" tone="info" value={formatMb(p.p50)} sub="در یک روز (میانه)" />
        <Stat index={2} icon={<Gauge className="size-4" />} label="۹۰٪ کاربران کمتر از" tone="warning" value={formatMb(p.p90)} sub="در یک روز" />
        <Stat index={3} icon={<Gauge className="size-4" />} label="۹۹٪ کاربران کمتر از" tone="danger" value={formatMb(p.p99)} sub="پرمصرف‌ترین ۱٪ بیشتر از این" />
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <ChartCard icon={<Globe2 className="size-5" />} title="کشورها" sentence={`میانگین کاربر یکتای روزانه؛ کشورهای کمتر از ${formatNumber(usage.min_users)} نفر در «بقیه».`}>
          <RankedBars items={asBars(usage.by_country, countryName)} format={(v) => formatNumber(v)} label="کاربران به تفکیک کشور" />
        </ChartCard>
        <ChartCard icon={<MapPinned className="size-5" />} title="استان‌ها" sentence="از پایگاه جغرافیایی آفلاین روی خود سرور.">
          {usage.by_region.length ? <RankedBars items={asBars(usage.by_region, regionName)} format={(v) => formatNumber(v)} label="کاربران به تفکیک استان" /> : <p className="text-sm text-fg-muted">هنوز گزارشی نیست.</p>}
        </ChartCard>
        <ChartCard icon={<Radio className="size-5" />} title="اپراتورها" sentence="به تفکیک شبکه (ASN).">
          <RankedBars items={asBars(usage.by_asn, ispName)} format={(v) => formatNumber(v)} label="کاربران به تفکیک اپراتور" />
        </ChartCard>
      </div>
      <div className="grid gap-4 lg:grid-cols-12">
        <ChartCard
          className="lg:col-span-7"
          icon={<Activity className="size-5" />}
          title="ساعت‌های اوج"
          sentence={peak ? `شلوغ‌ترین ساعت: ساعت ${formatNumber(peak.hour)} به وقت تهران.` : null}
        >
          <UsageHeatmap cells={usage.heatmap} />
        </ChartCard>
        <ChartCard className="lg:col-span-5" icon={<Server className="size-5" />} title="پرمصرف‌ترین پروکسی‌ها و کانفیگ‌ها" sentence="هر ردیف یک پروکسی مشترک است، نه یک نفر.">
          <div className="overflow-x-auto rounded-2xl border border-line">
            <table className="w-full text-sm">
              <thead className="bg-surface-2/70">
                <tr>
                  <th className={th}>نام</th>
                  <th className={th}>نوع</th>
                  <th className={th}>دریافت کاربران</th>
                  <th className={th}>اوج اتصال</th>
                </tr>
              </thead>
              <tbody>
                {usage.proxies.map((x) => (
                  <tr key={`${x.kind}-${x.ref}`} className="border-t border-line">
                    <td className={cn(td, "ltr text-right")}>{x.ref}</td>
                    <td className={td}>{x.kind === "mtproto" ? "MTProto" : "Reality"}</td>
                    <td className={td}>{formatBytes(x.bytes_down)}</td>
                    <td className={td}>{formatNumber(x.conns_max)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ChartCard>
      </div>
    </Section>
  );
}

// --- channel growth and sources ------------------------------------------------------------------------------

function ChannelSection({ s, series }: { s: AnalyticsSummary; series: AnalyticsSeries | undefined }) {
  const main = s.channel.main;
  const test = s.channel.test;
  const sources = series?.sources ?? s.top_sources;
  const days = series?.days ?? [];
  const totalJoins = days.reduce((n, d) => n + (d.joins ?? 0), 0);
  const totalLeaves = days.reduce((n, d) => n + (d.leaves ?? 0), 0);
  return (
    <Section id="stats-channel" icon={<UsersRound className="size-5" />} title="رشد کانال و منبع عضو" sentence={main ? membersSentence(main) : null}>
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat
          index={0}
          icon={<UsersRound className="size-4" />}
          label="اعضای کانال اصلی"
          value={main?.members === null || !main ? "—" : formatNumber(main.members)}
          sub={main?.members_at ? `شمارش ${formatRelative(main.members_at)}` : "هنوز شمرده نشده"}
        />
        <Stat index={1} icon={<Plus className="size-4" />} label="خالص امروز" tone="success" value={main ? formatSigned(main.net_today) : "—"} sub={main ? `${formatNumber(main.joins_today)} ورود، ${formatNumber(main.leaves_today)} خروج` : undefined} />
        <Stat index={2} icon={<Activity className="size-4" />} label="خالص ۷ روز" tone="info" value={main ? formatSigned(main.net_7d) : "—"} sub={main ? `${formatNumber(main.joins_7d)} ورود، ${formatNumber(main.leaves_7d)} خروج` : undefined} />
        <Stat
          index={3}
          icon={<Activity className="size-4" />}
          label="خالص ۳۰ روز"
          tone="brand"
          value={main ? formatSigned(main.net_30d) : "—"}
          sub={test?.members !== null && test ? `کانال آزمایشی: ${formatNumber(test.members)} عضو` : main ? `${formatNumber(main.joins_30d)} ورود، ${formatNumber(main.leaves_30d)} خروج` : undefined}
        />
      </div>
      {!main ? (
        <Card>
          <NoData icon={<Megaphone className="size-7" />} title="کانال اصلی ثبت نشده">
            کانال اصلی را در صفحه‌ی تلگرام ثبت کنید. از همان لحظه تعداد اعضا هر ساعت و هر ورود و خروج (با منبع) ثبت میشود.
          </NoData>
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-12">
          <ChartCard
            className="lg:col-span-6"
            icon={<UsersRound className="size-5" />}
            title="اعضا در طول زمان"
            sentence={series && series.members.length > 1 ? `از ${formatNumber(series.members[0]?.count ?? 0)} به ${formatNumber(series.members.at(-1)?.count ?? 0)} عضو در این بازه.` : "شمارش ساعتی اعضا تازه شروع شده است."}
          >
            {series && series.members.length > 0 ? <MembersChart points={series.members} /> : <p className="text-sm text-fg-muted">هنوز شمارشی نیست.</p>}
          </ChartCard>
          <ChartCard
            className="lg:col-span-6"
            icon={<Activity className="size-5" />}
            title="ورود و خروج روزانه"
            sentence={totalJoins + totalLeaves > 0 ? `در این بازه ${formatNumber(totalJoins)} نفر آمدند و ${formatNumber(totalLeaves)} نفر رفتند (خالص ${formatSigned(totalJoins - totalLeaves)}).` : "در این بازه هنوز ورود یا خروجی ثبت نشده است."}
            legend={
              <ChartLegend
                items={[
                  { name: "ورود", color: "series1" },
                  { name: "خروج", color: "series2" },
                ]}
              />
            }
          >
            <JoinsLeavesChart days={days} />
          </ChartCard>
          <ChartCard className="lg:col-span-5" icon={<Link2 className="size-5" />} title="از کجا آمدند" sentence={sourcesSentence(sources) ?? "هنوز عضوی با منبع ثبت نشده است."}>
            {sources.length > 0 ? (
              <>
                <RankedBars items={sources.map((x) => ({ label: sourceLabel(x), value: x.joins }))} format={(v) => `${formatNumber(v)} نفر`} label="عضو تازه به تفکیک منبع" />
                <TableDetails summary="ورود، خروج و خالص هر منبع">
                  <table className="w-full">
                    <thead>
                      <tr>
                        <th className={th}>منبع</th>
                        <th className={th}>ورود</th>
                        <th className={th}>خروج</th>
                        <th className={th}>خالص</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sources.map((x) => (
                        <tr key={x.key} className="border-t border-line">
                          <td className={td}>{sourceLabel(x)}</td>
                          <td className={td}>{formatNumber(x.joins)}</td>
                          <td className={td}>{formatNumber(x.leaves)}</td>
                          <td className={td}>{formatSigned(x.net)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableDetails>
              </>
            ) : null}
          </ChartCard>
          <TrackingLinksCard className="lg:col-span-7" />
        </div>
      )}
    </Section>
  );
}

function TrackingLinksCard({ className }: { className?: string }) {
  const qc = useQueryClient();
  const links = useTrackingLinks();
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameId = useId();
  const purposeId = useId();

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const n = name.trim();
    if (n.length < 1 || n.length > 32) {
      setError("نام لینک باید ۱ تا ۳۲ نویسه باشد (سقف تلگرام).");
      return;
    }
    setBusy(true);
    setError(null);
    createTrackingLink(n, purpose.trim())
      .then((link) => {
        qc.setQueryData<TrackingLink[]>(analyticsKeys.links, (old) => [link, ...(old ?? [])]);
        void qc.invalidateQueries({ queryKey: ["events"] });
        setName("");
        setPurpose("");
        toast.success("لینک ردیابی ساخته شد", "لینک را کپی کنید و به صاحب کانال تبلیغ بدهید.");
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader
        icon={<Link2 className="size-5" />}
        title="لینک‌های ردیابی"
        subtitle="برای هر تبلیغ یک لینک دعوت با نام بسازید؛ هر کس با آن بیاید، اینجا شمرده میشود. لینک‌ها هرگز باطل یا پاک نمیشوند."
      />
      <div className="flex flex-col gap-4 p-4 sm:p-5">
        <form onSubmit={submit} noValidate className="grid gap-3 rounded-2xl border border-line bg-surface-2/60 p-3 sm:grid-cols-[1fr_1.4fr_auto] sm:items-end">
          <Field label="نام لینک" htmlFor={nameId} hint="حداکثر ۳۲ نویسه؛ مثلاً «کانال الف - آبان»">
            <TextInput id={nameId} value={name} maxLength={32} onChange={(e) => setName(e.target.value)} placeholder="کانال الف - آبان" />
          </Field>
          <Field label="توضیح (اختیاری)" htmlFor={purposeId} hint="کجا، چه تاریخی، چقدر پرداخت شد">
            <TextInput id={purposeId} value={purpose} maxLength={200} onChange={(e) => setPurpose(e.target.value)} placeholder="پست سنجاق‌شده، ۲ روز" />
          </Field>
          <Button type="submit" loading={busy} icon={<Plus className="size-4" />} className="sm:mb-7">
            ساخت لینک ردیابی
          </Button>
          {error ? (
            <p role="alert" className="text-sm leading-7 text-danger sm:col-span-3">
              {error}
            </p>
          ) : null}
        </form>
        {links.isError ? (
          <LoadError error={links.error} onRetry={() => void links.refetch()} compact />
        ) : links.isPending ? (
          <SkeletonLines lines={3} />
        ) : links.data.length === 0 ? (
          <p className="text-sm leading-7 text-fg-muted">هنوز لینکی ساخته نشده است.</p>
        ) : (
          <ul className="flex flex-col gap-2.5">
            {links.data.map((l) => (
              <LinkRow key={l.id} link={l} />
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function LinkRow({ link }: { link: TrackingLink }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(link.name);
  const [purpose, setPurpose] = useState(link.purpose);
  const [busy, setBusy] = useState(false);
  const id = useId();

  const save = () => {
    if (busy) return;
    setBusy(true);
    renameTrackingLink(link.id, name.trim(), purpose.trim())
      .then((updated) => {
        qc.setQueryData<TrackingLink[]>(analyticsKeys.links, (old) => (old ?? []).map((x) => (x.id === updated.id ? updated : x)));
        void qc.invalidateQueries({ queryKey: analyticsKeys.summary });
        setEditing(false);
        toast.success("نام لینک عوض شد", "آمار قبلی لینک سر جایش است.");
      })
      .catch((err: unknown) => toast.error("نام عوض نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <li className="flex flex-col gap-2 rounded-2xl border border-line bg-surface p-3">
      {editing ? (
        <div className="flex flex-col gap-2">
          <label htmlFor={`${id}-n`} className="sr-only">
            نام تازه
          </label>
          <TextInput id={`${id}-n`} value={name} maxLength={32} onChange={(e) => setName(e.target.value)} />
          <label htmlFor={`${id}-p`} className="sr-only">
            توضیح
          </label>
          <TextInput id={`${id}-p`} value={purpose} maxLength={200} onChange={(e) => setPurpose(e.target.value)} />
          <div className="flex gap-2">
            <Button size="sm" loading={busy} icon={<Check className="size-4" />} onClick={save}>
              ذخیره
            </Button>
            <Button size="sm" variant="ghost" icon={<X className="size-4" />} onClick={() => setEditing(false)}>
              انصراف
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate font-bold text-fg">{link.name}</div>
            {link.purpose ? <div className="text-xs leading-6 text-fg-muted">{link.purpose}</div> : null}
          </div>
          <button type="button" onClick={() => setEditing(true)} className="grid size-8 shrink-0 place-items-center rounded-lg text-fg-faint hover:bg-surface-3 hover:text-fg" aria-label={`تغییر نام ${link.name}`}>
            <Pencil className="size-4" />
          </button>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <code className="ltr min-w-0 flex-1 truncate rounded-lg bg-surface-2 px-2 py-1.5 text-left text-xs text-fg">{link.invite_link}</code>
        <CopyButton text={link.invite_link} label="کپی لینک" />
      </div>
      <div className="flex flex-wrap gap-1.5 text-xs">
        <Badge tone="success">{formatNumber(link.joins)} ورود</Badge>
        <Badge tone="neutral">{formatNumber(link.leaves)} خروج</Badge>
        <Badge tone="brand">خالص {formatSigned(link.net)}</Badge>
        <Badge tone="info">۷ روز: {formatNumber(link.joins_7d)}</Badge>
        <span className="ms-auto self-center text-fg-faint">ساخته {formatJalali(link.created_at, "date")}</span>
      </div>
    </li>
  );
}

// --- post views and shares (D38) ---------------------------------------------------------------------------

function PostsSection({ q, outcome }: { q: ReturnType<typeof useChannelStats>; outcome: string | undefined }) {
  const note = channelStatsOutcomeText(outcome);
  if (q.isError) {
    return (
      <Section id="stats-posts" icon={<Eye className="size-5" />} title="بازدید و بازنشر پست‌ها">
        <Card>
          <LoadError error={q.error} onRetry={() => void q.refetch()} compact />
        </Card>
      </Section>
    );
  }
  const main = q.data?.main ?? null;
  const posts = q.data?.posts ?? [];
  const sentence = postsSentence(main);
  return (
    <Section id="stats-posts" icon={<Eye className="size-5" />} title="بازدید و بازنشر پست‌ها" sentence={sentence}>
      {!main && posts.length === 0 ? (
        <Card>
          <NoData icon={<Eye className="size-7" />} title="هنوز آماری از تلگرام نیست">
            بازدید هر پست، بازنشرها، منبع بازدید و عضو تازه، زبان اعضا و ساعت‌های اوج را خود تلگرام فقط به یک حساب کاربری ادمین نشان میدهد. پنل آن را هر سه
            ساعت با حساب دوم (نه حساب شخصی شما) میخواند. {note ?? ""}
          </NoData>
        </Card>
      ) : (
        <>
          {main ? (
            <>
              {main.status !== "ok" ? <p className="rounded-2xl bg-info-soft p-3 text-sm leading-7 text-fg">{channelStatsStatusText(main.status)}</p> : null}
              <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
                <Stat index={0} icon={<Eye className="size-4" />} label="بازدید هر پست" value={main.views_per_post === null ? "—" : formatNumber(main.views_per_post)} sub={main.views_per_post_prev === null ? undefined : `دوره‌ی قبل: ${formatNumber(main.views_per_post_prev)}`} />
                <Stat index={1} icon={<Share2 className="size-4" />} label="بازنشر هر پست" tone="info" value={main.shares_per_post === null ? "—" : formatNumber(main.shares_per_post)} sub={main.shares_per_post_prev === null ? undefined : `دوره‌ی قبل: ${formatNumber(main.shares_per_post_prev)}`} />
                <Stat index={2} icon={<Sparkles className="size-4" />} label="واکنش هر پست" tone="warning" value={main.reactions_per_post === null ? "—" : formatNumber(main.reactions_per_post)} />
                <Stat index={3} icon={<UsersRound className="size-4" />} label="اعلان روشن" tone="success" value={formatPct(main.notifications_pct)} sub={main.participants === null ? undefined : `${formatNumber(main.participants)} عضو`} />
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                {main.graphs.followers ? (
                  <ChartCard icon={<UsersRound className="size-5" />} title="عضو کانال (از تلگرام)">
                    <TelegramGraph graph={main.graphs.followers} label="نمودار عضو کانال از آمار تلگرام" />
                  </ChartCard>
                ) : null}
                {main.graphs.new_followers_by_source ? (
                  <ChartCard icon={<Link2 className="size-5" />} title="عضو تازه به تفکیک منبع (از تلگرام)">
                    <TelegramGraph graph={main.graphs.new_followers_by_source} label="عضو تازه به تفکیک منبع" />
                  </ChartCard>
                ) : null}
                {main.graphs.views_by_source ? (
                  <ChartCard icon={<Eye className="size-5" />} title="بازدید به تفکیک منبع">
                    <TelegramGraph graph={main.graphs.views_by_source} label="بازدید به تفکیک منبع" />
                  </ChartCard>
                ) : null}
                {main.graphs.languages ? (
                  <ChartCard icon={<Globe2 className="size-5" />} title="زبان اعضا">
                    <TelegramGraph graph={main.graphs.languages} label="زبان اعضای کانال" />
                  </ChartCard>
                ) : null}
              </div>
            </>
          ) : null}
          {posts.length > 0 ? (
            <Card>
              <CardHeader icon={<Megaphone className="size-5" />} title="پست‌های اخیر" subtitle="بازدید و بازنشر هر پست ما (بی افزودن بازدید)." />
              <div className="overflow-x-auto p-4 sm:p-5">
                <table className="w-full text-sm">
                  <thead className="bg-surface-2/70">
                    <tr>
                      <th className={th}>پست</th>
                      <th className={th}>زمان</th>
                      <th className={th}>بازدید</th>
                      <th className={th}>بازنشر</th>
                      <th className={th}>واکنش</th>
                    </tr>
                  </thead>
                  <tbody>
                    {posts.map((p) => (
                      <tr key={p.message_id} className="border-t border-line">
                        <td className={td}>شماره {formatNumber(p.message_id)}</td>
                        <td className={cn(td, "text-fg-muted")}>{p.posted_at ? formatJalali(p.posted_at, "short") : "—"}</td>
                        <td className={td}>{formatNumber(p.views)}</td>
                        <td className={td}>{formatNumber(p.forwards)}</td>
                        <td className={td}>{p.reactions === null ? "—" : formatNumber(p.reactions)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          ) : null}
        </>
      )}
    </Section>
  );
}

// --- capacity and thresholds ---------------------------------------------------------------------------------

function CapacitySection({ s, series }: { s: AnalyticsSummary; series: AnalyticsSeries | undefined }) {
  const live = s.servers.filter((x) => x.state !== "deleted");
  const hot = live.filter((x) => x.signal !== "ok" && x.fresh).length;
  const sentence =
    live.length === 0
      ? "هنوز سرور زنده‌ای نیست."
      : hot === 0
        ? `هر ${formatNumber(live.length)} سرور زیر آستانه‌ها کار میکنند.`
        : `${formatNumber(hot)} سرور از ${formatNumber(live.length)} سرور به آستانه رسیده‌اند.`;
  const cpu = series?.cpu ?? [];
  return (
    <Section id="stats-capacity" icon={<Gauge className="size-5" />} title="ظرفیت و آستانه‌ها" sentence={sentence}>
      <div className="grid gap-4 lg:grid-cols-12">
        <ChartCard
          className="lg:col-span-7"
          icon={<Activity className="size-5" />}
          title="CPU سرورها"
          sentence={cpu.length ? "میانگین همه‌ی سرورها و پرکارترین سرور، ساعت به ساعت (درصد کل سرور)." : "هنوز CPU سنجیده نشده است."}
          legend={
            cpu.length ? (
              <ChartLegend
                items={[
                  { name: "میانگین همه", color: "series1" },
                  { name: "پرکارترین سرور", color: "series2" },
                ]}
              />
            ) : null
          }
        >
          {cpu.length ? <CpuChart points={cpu} threshold={s.thresholds.cpu_warn_pct} /> : null}
          {live.length > 0 ? (
            <ul className="grid gap-2 sm:grid-cols-2">
              {live.map((x) => {
                const sig = SIGNAL_TEXT[x.signal];
                return (
                  <li key={x.node_id} className="flex items-center justify-between gap-2 rounded-2xl border border-line bg-surface-2/60 px-3 py-2 text-sm">
                    <span className="min-w-0 truncate font-semibold text-fg">{x.name}</span>
                    <span className="flex shrink-0 items-center gap-2 text-xs text-fg-muted">
                      {x.net_out_bps !== null ? formatBps(x.net_out_bps) : null}
                      <Badge tone={x.fresh ? sig.tone : "neutral"} dot>
                        {x.fresh ? sig.label : "بی داده‌ی تازه"}
                      </Badge>
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </ChartCard>
        <ThresholdsCard className="lg:col-span-5" />
      </div>
    </Section>
  );
}

const WINDOWS = [15, 30, 60, 180];

function ThresholdsCard({ className }: { className?: string }) {
  const q = useAnalyticsSettings();
  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader icon={<TriangleAlert className="size-5" />} title="آستانه‌های هشدار" subtitle="رسیدن به هر آستانه، روزی یک بار برای هر سرور، در چت تلگرام شما خبر داده میشود." />
      <div className="p-4 sm:p-5">
        {q.isError ? <LoadError error={q.error} onRetry={() => void q.refetch()} compact /> : q.isPending ? <SkeletonLines lines={4} /> : <ThresholdsForm initial={q.data} />}
      </div>
    </Card>
  );
}

/** Edits a copy of the stored thresholds (background refetches never overwrite what is typed). */
function ThresholdsForm({ initial }: { initial: Thresholds }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<Thresholds>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trafficId = useId();
  const cpuId = useId();
  const windowId = useId();

  const save = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    if (![form.traffic_warn_pct, form.cpu_warn_pct].every((v) => Number.isInteger(v) && v >= 1 && v <= 100)) {
      setError("آستانه‌ها باید عددی بین ۱ تا ۱۰۰ باشند.");
      return;
    }
    setBusy(true);
    setError(null);
    putAnalyticsSettings(form)
      .then((saved) => {
        qc.setQueryData(analyticsKeys.settings, saved);
        setForm(saved);
        void qc.invalidateQueries({ queryKey: analyticsKeys.summary });
        toast.success("آستانه‌ها ذخیره شد");
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };

  const num = (v: string) => (v.trim() === "" ? Number.NaN : Number(v.replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)))));

  return (
    <form onSubmit={save} noValidate className="flex flex-col gap-4">
      <Field label="هشدار ترافیک (درصد سقف رایگان ماه)" htmlFor={trafficId}>
        <TextInput id={trafficId} inputMode="numeric" value={Number.isNaN(form.traffic_warn_pct) ? "" : String(form.traffic_warn_pct)} onChange={(e) => setForm({ ...form, traffic_warn_pct: num(e.target.value) })} suffix="٪" />
      </Field>
      <Field label="هشدار CPU (درصد کل سرور)" htmlFor={cpuId}>
        <TextInput id={cpuId} inputMode="numeric" value={Number.isNaN(form.cpu_warn_pct) ? "" : String(form.cpu_warn_pct)} onChange={(e) => setForm({ ...form, cpu_warn_pct: num(e.target.value) })} suffix="٪" />
      </Field>
      <div className="flex flex-col gap-1.5">
        <span id={windowId} className="text-sm font-semibold text-fg">
          میانگین CPU در چه مدت؟
        </span>
        <Segmented
          label="بازه‌ی میانگین CPU"
          value={String(form.cpu_window_min)}
          onChange={(v) => setForm({ ...form, cpu_window_min: Number(v) })}
          options={WINDOWS.map((w) => ({ value: String(w), label: w < 60 ? `${formatNumber(w)} دقیقه` : `${formatNumber(w / 60)} ساعت` }))}
          className="w-full overflow-x-auto"
        />
      </div>
      <Toggle checked={form.alerts_enabled} onCheckedChange={(v) => setForm({ ...form, alerts_enabled: v })} label="هشدار در تلگرام" description="خاموش: هشدارها فقط در همین صفحه دیده میشوند." />
      {error ? (
        <p role="alert" className="text-sm leading-7 text-danger">
          {error}
        </p>
      ) : null}
      <Button type="submit" loading={busy} icon={<Save className="size-4" />} className="self-start">
        ذخیره‌ی آستانه‌ها
      </Button>
    </form>
  );
}

// --- page ----------------------------------------------------------------------------------------------------

export default function StatsPage() {
  const [days, setDays] = useState<Days>("30");
  const summary = useAnalyticsSummary();
  const series = useAnalyticsSeries(Number(days));
  const usage = useAnalyticsUsage(Number(days));
  const channelStats = useChannelStats();

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="آمار"
        eyebrow="ترافیک، هزینه، کاربران و رشد کانال"
        subtitle="عددها از خود هتزنر، تلگرام و عامل سرورها؛ همه آمار کلی، بی IP و بی ردیابی کاربر پروکسی."
        actions={
          <Segmented
            label="بازه‌ی نمودارها"
            value={days}
            onChange={setDays}
            options={[
              { value: "7", label: "۷ روز" },
              { value: "30", label: "۳۰ روز" },
              { value: "90", label: "۹۰ روز" },
            ]}
          />
        }
      />
      {summary.isError ? (
        <Card>
          <LoadError
            error={summary.error}
            onRetry={() => void summary.refetch()}
            notYet={{ title: "صفحه‌ی آمار هنوز روی پنل فعال نشده", description: "با به‌روزرسانی بعدی پنل خودش فعال میشود." }}
          />
        </Card>
      ) : summary.isPending ? (
        <div className="flex flex-col gap-4" role="status" aria-label="در حال بارگذاری آمار">
          <Skeleton className="h-44 w-full rounded-3xl" />
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Skeleton className="h-28 rounded-3xl" />
            <Skeleton className="h-28 rounded-3xl" />
            <Skeleton className="h-28 rounded-3xl" />
            <Skeleton className="h-28 rounded-3xl" />
          </div>
        </div>
      ) : (
        <>
          <Headline s={summary.data} usage={usage.data} stats={channelStats.data} />
          <Warnings s={summary.data} />
          <div className={cn("flex flex-col gap-8 transition-opacity", series.isPlaceholderData && "opacity-60")}>
            <TrafficSection s={summary.data} series={series.data} />
            <UsersSection usage={usage.data} isError={usage.isError} error={usage.error} refetch={() => void usage.refetch()} />
            <ChannelSection s={summary.data} series={series.data} />
            <PostsSection q={channelStats} outcome={summary.data.collector.channel_stats?.outcome} />
            <CapacitySection s={summary.data} series={series.data} />
          </div>
        </>
      )}
    </div>
  );
}
