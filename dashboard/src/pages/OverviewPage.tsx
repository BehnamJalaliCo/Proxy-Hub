import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowLeft,
  Ban,
  CircleCheck,
  CircleDashed,
  CircleX,
  Coins,
  FlaskConical,
  HeartPulse,
  ListChecks,
  OctagonPause,
  RefreshCw,
  Server,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  UsersRound,
  Wallet,
  Workflow,
} from "lucide-react";
import { motion } from "motion/react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { ActivityChart, ActivityLegend } from "../components/charts/ActivityChart";
import { BudgetMeter } from "../components/charts/BudgetMeter";
import { EventRow } from "../components/EventRow";
import { Badge, Card, CardHeader, StatusDot } from "../components/ui/Card";
import { AnimatedNumber, EmptyState, ErrorState, Skeleton, SkeletonLines } from "../components/ui/Feedback";
import { IconButton } from "../components/ui/Button";
import { PageHeader } from "../components/ui/PageHeader";
import { cn } from "../lib/browser";
import { decimalToNumber, formatDecimal, formatDurationSeconds, formatJalali, formatNumber, formatPercent, formatRelative } from "../lib/format";
import { planActionInfo, reasonLabel, type Tone } from "../lib/labels";
import { groupPlan } from "../lib/plan";
import { useFleetPlan, useOverview, useRecentEvents } from "../lib/queries";
import { TONE_SOFT } from "../lib/tones";
import type { Overview } from "../lib/types";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

const fmtInt = (n: number) => formatNumber(Math.round(n));

// --- hero: cost per member ------------------------------------------------------------------

function CostPerMemberHero({ overview }: { overview: Overview | undefined }) {
  return (
    <Card className="ph-glow-border relative overflow-clip lg:col-span-8">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 -left-24 size-72 rounded-full bg-[radial-gradient(circle,var(--ph-bg-glow-1),transparent_70%)]"
      />
      <div className="relative flex flex-col gap-6 p-5 sm:p-7">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="grid size-12 place-items-center rounded-2xl bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))] text-white shadow-[0_10px_30px_-12px_var(--ph-brand)]">
              <Coins className="size-6" />
            </span>
            <div>
              <h2 className="text-lg leading-8 font-extrabold text-fg">هزینه‌ی هر عضو</h2>
              <p className="text-sm text-fg-muted">مهم‌ترین عدد پنل: خرج ماه ÷ عضو تازه‌ی ماندگار کانال</p>
            </div>
          </div>
          <Badge tone="neutral">به‌زودی</Badge>
        </div>

        <div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="flex items-baseline gap-3" aria-label="هزینه‌ی هر عضو: هنوز داده‌ای نیست">
              <span className="text-[3.5rem] leading-none font-black tracking-tight text-fg-faint/60 sm:text-[4.25rem]">−−</span>
              <span className="text-lg font-bold text-fg-faint">€ برای هر عضو</span>
            </div>
            <p className="mt-3 flex max-w-md items-start gap-2 text-sm leading-7 text-fg-muted">
              <Sparkles className="mt-1 size-4 shrink-0 text-brand" />
              هنوز داده‌ی رشد نیست. وقتی کانال و ثبت عضوهای تازه راه بیفتد (فاز رشد)، این عدد از داده‌ی واقعی حساب میشود و
              اینجا بزرگ‌ترین عدد پنل است.
            </p>
          </div>
          <dl className="grid grid-cols-3 gap-2 sm:w-[24rem]">
            <MiniStat label="خرج پیش‌بینی ماه" value={overview ? `${formatDecimal(overview.month_projected_eur)} €` : "…"} live />
            <MiniStat label="عضو تازه‌ی امروز" value="—" />
            <MiniStat label="ماندگاری ۷ روزه" value="—" />
          </dl>
        </div>
      </div>
    </Card>
  );
}

function MiniStat({ label, value, live = false }: { label: string; value: string; live?: boolean }) {
  return (
    <div className={cn("rounded-2xl border border-line p-3", live ? "bg-surface" : "bg-surface-2/60")}>
      <dt className="text-[0.7rem] leading-5 text-fg-muted">{label}</dt>
      <dd className={cn("mt-1 text-base font-extrabold", live ? "text-fg" : "text-fg-faint")}>{value}</dd>
    </div>
  );
}

// --- kill switch ------------------------------------------------------------------------------

function KillSwitchStatus({ overview }: { overview: Overview }) {
  const on = overview.kill_switch;
  return (
    <Card
      className={cn(
        "relative flex flex-col overflow-clip lg:col-span-4",
        on ? "border-danger/30" : "border-success/30",
      )}
    >
      <div
        aria-hidden
        className={cn(
          "absolute inset-0 opacity-90",
          on
            ? "bg-[radial-gradient(120%_80%_at_100%_0%,var(--ph-danger-soft),transparent_70%)]"
            : "bg-[radial-gradient(120%_80%_at_100%_0%,var(--ph-success-soft),transparent_70%)]",
        )}
      />
      <div className="relative flex flex-1 flex-col gap-4 p-5 sm:p-6">
        <div className="flex items-center gap-3">
          <span className={cn("relative grid size-14 place-items-center rounded-2xl", on ? "bg-danger text-white" : "bg-success text-white")}>
            {on ? <OctagonPause className="size-7" /> : <ShieldCheck className="size-7" />}
            <span className={cn("absolute inset-0 animate-ping-soft rounded-2xl", on ? "bg-danger/40" : "bg-success/30")} aria-hidden />
          </span>
          <div>
            <div className="text-xs font-semibold text-fg-muted">کلید توقف</div>
            <div className={cn("text-xl font-extrabold", on ? "text-danger" : "text-success")}>{on ? "روشن" : "خاموش"}</div>
          </div>
        </div>
        <p className="text-sm leading-7 text-fg">
          {on
            ? "همه‌ی ساخت و حذف سرورها متوقف است. پنل فقط میخواند و برنامه میریزد."
            : "پنل اجازه دارد در محدوده‌ی سقف بودجه و سقف تعداد، سرور بسازد و حذف کند."}
        </p>
        <div className="mt-auto flex flex-wrap items-center gap-2">
          <Badge tone={overview.dry_run ? "warning" : "success"} dot>
            {overview.dry_run ? "حالت خشک (آزمایشی)" : "حالت واقعی"}
          </Badge>
          <Link to="/settings" className="ms-auto inline-flex items-center gap-1 text-sm font-bold text-brand hover:underline">
            مدیریت
            <ArrowLeft className="size-4" />
          </Link>
        </div>
      </div>
    </Card>
  );
}

// --- KPI tiles ----------------------------------------------------------------------------------

function Kpi({
  icon,
  label,
  children,
  sub,
  tone = "brand",
  index,
}: {
  icon: ReactNode;
  label: string;
  children: ReactNode;
  sub: ReactNode;
  tone?: Tone;
  index: number;
}) {
  return (
    <motion.div {...fadeUp(index)}>
      <Card className="group h-full p-4 transition-transform duration-300 hover:-translate-y-0.5 sm:p-5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.8rem] font-semibold text-fg-muted">{label}</span>
          <span className={cn("grid size-8 place-items-center rounded-xl", TONE_SOFT[tone])}>{icon}</span>
        </div>
        <div className="mt-3 text-[1.9rem] leading-none font-extrabold tracking-tight text-fg sm:text-[2.2rem]">{children}</div>
        <div className="mt-2 text-xs leading-6 text-fg-muted">{sub}</div>
      </Card>
    </motion.div>
  );
}

function KpiRow({ o }: { o: Overview }) {
  const age = o.worker_heartbeat_age_s;
  const workerTone: Tone = age === null ? "danger" : age > 90 ? "warning" : "success";
  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
      <Kpi index={0} icon={<Server className="size-4" />} label="node زنده" sub={`از ${formatNumber(o.target_slots)} جایگاه هدف`}>
        <AnimatedNumber value={o.nodes_live} format={fmtInt} />
      </Kpi>
      <Kpi
        index={1}
        icon={<Wallet className="size-4" />}
        label="خرج پیش‌بینی ماه"
        tone="info"
        sub={o.budget_cap_eur ? `سقف: ${formatDecimal(o.budget_cap_eur)} یورو` : "سقف بودجه تعیین نشده"}
      >
        <span>{formatDecimal(o.month_projected_eur)}</span>
        <span className="ms-1 text-base font-bold text-fg-faint">€</span>
      </Kpi>
      <Kpi
        index={2}
        icon={<UsersRound className="size-4" />}
        label="حساب‌های فعال"
        tone="success"
        sub={`از ${formatNumber(o.accounts.total)} حساب${o.accounts.locked ? ` · ${formatNumber(o.accounts.locked)} قفل` : ""}`}
      >
        <AnimatedNumber value={o.accounts.active} format={fmtInt} />
      </Kpi>
      <Kpi
        index={3}
        icon={<HeartPulse className="size-4" />}
        label="ضربان worker"
        tone={workerTone}
        sub={`آخرین برنامه‌ی ناوگان: ${o.last_fleet_run_at ? formatRelative(o.last_fleet_run_at) : "هنوز نه"}`}
      >
        <span className="flex items-center gap-2 text-[1.35rem] sm:text-[1.5rem]">
          <StatusDot tone={workerTone} pulse={workerTone === "success"} className="size-2.5" />
          {age === null ? "بی ضربان" : formatDurationSeconds(age)}
        </span>
      </Kpi>
    </div>
  );
}

// --- fleet slots ----------------------------------------------------------------------------------

function SlotGrid({ o }: { o: Overview }) {
  const shown = Math.min(o.target_slots, 40);
  const filled = Math.min(o.nodes_live, shown);
  const extra = Math.max(0, o.nodes_live - o.target_slots);
  const cols = shown <= 10 ? "grid-cols-5" : shown <= 20 ? "grid-cols-5 sm:grid-cols-10" : "grid-cols-8 sm:grid-cols-10";
  return (
    <Card className="lg:col-span-5">
      <CardHeader
        icon={<Server className="size-5" />}
        title="جایگاه‌های ناوگان"
        subtitle={`${formatNumber(o.nodes_live)} node زنده در ${formatNumber(o.target_slots)} جایگاه هدف`}
      />
      <div className="p-5">
        {o.target_slots === 0 ? (
          <EmptyState icon={<CircleDashed className="size-7" />} title="هیچ جایگاهی تعیین نشده" description="تعداد جایگاه هدف را در تنظیمات ناوگان بگذارید." />
        ) : (
          <>
            <ul className={cn("grid gap-2.5", cols)} aria-label="جایگاه‌ها">
              {Array.from({ length: shown }, (_, i) => {
                const live = i < filled;
                return (
                  <motion.li
                    key={i}
                    initial={{ opacity: 0, scale: 0.6 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ delay: 0.03 * i, type: "spring", stiffness: 320, damping: 22 }}
                    className={cn(
                      "relative grid aspect-square place-items-center rounded-2xl",
                      live
                        ? "bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))] text-white shadow-[0_8px_22px_-10px_var(--ph-brand)]"
                        : "border-2 border-dashed border-line-strong text-fg-faint",
                    )}
                    aria-label={live ? `جایگاه ${formatNumber(i + 1)}: node زنده` : `جایگاه ${formatNumber(i + 1)}: خالی`}
                  >
                    {live ? <Server className="size-5" /> : <span className="text-xs font-bold">{formatNumber(i + 1)}</span>}
                  </motion.li>
                );
              })}
            </ul>
            <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-fg-muted">
              <span className="flex items-center gap-1.5">
                <span className="size-3 rounded bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))]" /> node زنده
              </span>
              <span className="flex items-center gap-1.5">
                <span className="size-3 rounded border-2 border-dashed border-line-strong" /> جایگاه خالی
              </span>
              {extra > 0 ? <Badge tone="warning">{formatNumber(extra)} node بیش از هدف</Badge> : null}
              <span className="ms-auto">
                سقف کل: {o.node_cap_total === null ? <b className="text-warning">تعیین نشده</b> : <b className="text-fg">{formatNumber(o.node_cap_total)}</b>}
              </span>
            </div>
            {o.target_slots > shown ? (
              <p className="mt-2 text-xs text-fg-faint">فقط {formatNumber(shown)} جایگاه اول نشان داده شده است.</p>
            ) : null}
          </>
        )}
      </div>
    </Card>
  );
}

// --- budget ---------------------------------------------------------------------------------------

function BudgetCard({ o }: { o: Overview }) {
  const cap = decimalToNumber(o.budget_cap_eur);
  const spent = decimalToNumber(o.month_projected_eur) ?? 0;
  if (cap === null || cap <= 0) {
    return (
      <Card className="lg:col-span-4">
        <CardHeader icon={<Wallet className="size-5" />} title="بودجه‌ی ماه" subtitle="خرج پیش‌بینی‌شده در برابر سقف" />
        <EmptyState
          icon={<TriangleAlert className="size-7" />}
          title="سقف بودجه تعیین نشده"
          description="تا سقف بودجه‌ی ماه تعیین نشود، پنل هیچ سروری نمیسازد."
          action={
            <Link to="/settings" className="text-sm font-bold text-brand hover:underline">
              تعیین سقف در تنظیمات
            </Link>
          }
        />
      </Card>
    );
  }
  const ratio = spent / cap;
  const state =
    ratio >= 1
      ? { tone: "danger" as const, icon: <CircleX className="size-3.5" />, text: "به سقف رسیده" }
      : ratio >= 0.8
        ? { tone: "warning" as const, icon: <TriangleAlert className="size-3.5" />, text: "نزدیک سقف" }
        : { tone: "success" as const, icon: <CircleCheck className="size-3.5" />, text: "در محدوده" };
  return (
    <Card className="lg:col-span-4">
      <CardHeader icon={<Wallet className="size-5" />} title="بودجه‌ی ماه" subtitle="خرج پیش‌بینی‌شده در برابر سقف" />
      <div className="relative px-5 pb-5">
        <BudgetMeter ratio={ratio} label={`بودجه: ${formatPercent(ratio)} از سقف`} className="h-44" />
        <div className="pointer-events-none absolute inset-x-0 top-[5.5rem] flex flex-col items-center">
          <div className="text-3xl font-extrabold text-fg">{formatPercent(ratio)}</div>
          <div className="text-xs text-fg-muted">از سقف ماه</div>
        </div>
        <div className="mt-1 flex items-center justify-between gap-2 text-sm">
          <div>
            <div className="text-xs text-fg-faint">پیش‌بینی</div>
            <div className="font-bold text-fg">{formatDecimal(o.month_projected_eur)} €</div>
          </div>
          <Badge tone={state.tone} className="gap-1">
            {state.icon}
            {state.text}
          </Badge>
          <div className="text-left">
            <div className="text-xs text-fg-faint">سقف</div>
            <div className="font-bold text-fg">{formatDecimal(o.budget_cap_eur)} €</div>
          </div>
        </div>
      </div>
    </Card>
  );
}

// --- readiness ------------------------------------------------------------------------------------

function Readiness({ o }: { o: Overview }) {
  const checks = [
    { ok: o.budget_cap_eur !== null, label: "سقف بودجه‌ی ماه", hint: o.budget_cap_eur ? `${formatDecimal(o.budget_cap_eur)} یورو` : "تعیین نشده" },
    { ok: o.node_cap_total !== null, label: "سقف تعداد node", hint: o.node_cap_total === null ? "تعیین نشده" : formatNumber(o.node_cap_total) },
    { ok: o.accounts.active > 0, label: "حساب هتزنر فعال", hint: `${formatNumber(o.accounts.active)} حساب` },
    { ok: !o.kill_switch, label: "کلید توقف خاموش", hint: o.kill_switch ? "روشن است" : "خاموش" },
    { ok: (o.worker_heartbeat_age_s ?? Infinity) <= 90, label: "worker سالم", hint: o.worker_heartbeat_age_s === null ? "بی ضربان" : formatDurationSeconds(o.worker_heartbeat_age_s) },
  ];
  const passed = checks.filter((c) => c.ok).length;
  return (
    <Card className="lg:col-span-3">
      <CardHeader icon={<ListChecks className="size-5" />} title="آمادگی ساخت node" subtitle={`${formatNumber(passed)} از ${formatNumber(checks.length)} شرط`} />
      <div className="px-5 pt-3">
        <div className="h-2 overflow-hidden rounded-full bg-brand-soft">
          <motion.div
            className="h-full rounded-full bg-[linear-gradient(90deg,var(--ph-brand-2),var(--ph-brand))]"
            initial={{ width: 0 }}
            animate={{ width: `${String((passed / checks.length) * 100)}%` }}
            transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }}
          />
        </div>
      </div>
      <ul className="flex flex-col gap-1 p-3">
        {checks.map((c) => (
          <li key={c.label} className="flex items-center gap-2.5 rounded-xl px-2 py-1.5 text-sm">
            {c.ok ? <CircleCheck className="size-[1.1rem] shrink-0 text-success" /> : <CircleX className="size-[1.1rem] shrink-0 text-danger" />}
            <span className="min-w-0 flex-1 text-fg">{c.label}</span>
            <span className={cn("text-xs", c.ok ? "text-fg-muted" : "font-semibold text-danger")}>{c.hint}</span>
          </li>
        ))}
      </ul>
      {o.dry_run ? (
        <p className="mx-5 mb-5 flex items-center gap-2 rounded-xl bg-warning-soft px-3 py-2 text-xs leading-6 text-fg">
          <FlaskConical className="size-4 shrink-0 text-warning" />
          در حالت خشک، حتی با همه‌ی شرط‌ها هیچ سروری ساخته نمیشود.
        </p>
      ) : null}
    </Card>
  );
}

// --- accounts -------------------------------------------------------------------------------------

function AccountsSplit({ o }: { o: Overview }) {
  const { active, locked, total } = o.accounts;
  const other = Math.max(0, total - active - locked);
  const parts = [
    { key: "active", label: "فعال", value: active, cls: "bg-success text-white" },
    { key: "locked", label: "قفل", value: locked, cls: "bg-danger text-white" },
    { key: "other", label: "دیگر وضعیت‌ها", value: other, cls: "bg-fg-faint text-white" },
  ].filter((p) => p.value > 0);
  return (
    <Card className="lg:col-span-4">
      <CardHeader
        icon={<UsersRound className="size-5" />}
        title="حساب‌های هتزنر"
        subtitle={`${formatNumber(total)} حساب`}
        action={
          <Link to="/accounts" className="inline-flex items-center gap-1 text-sm font-bold text-brand hover:underline">
            همه
            <ArrowLeft className="size-4" />
          </Link>
        }
      />
      <div className="p-5">
        {total === 0 ? (
          <EmptyState
            className="py-4"
            icon={<Server className="size-7" />}
            title="هنوز حسابی افزوده نشده"
            description="nodeها فقط در حساب‌های جدای هتزنر ساخته میشوند."
            action={
              <Link to="/accounts" className="text-sm font-bold text-brand hover:underline">
                افزودن اولین حساب
              </Link>
            }
          />
        ) : (
          <>
            <div
              className="flex h-7 w-full gap-0.5 overflow-hidden rounded-lg"
              role="img"
              aria-label={parts.map((p) => `${p.label}: ${formatNumber(p.value)}`).join("، ")}
            >
              {parts.map((p, i) => (
                <motion.div
                  key={p.key}
                  initial={{ flexGrow: 0 }}
                  animate={{ flexGrow: p.value }}
                  transition={{ delay: 0.1 * i, duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
                  style={{ flexBasis: 0 }}
                  className={cn("flex min-w-0 items-center rounded px-2 text-xs font-bold", p.cls)}
                  title={`${p.label}: ${formatNumber(p.value)}`}
                >
                  {p.value / total >= 0.15 ? formatNumber(p.value) : null}
                </motion.div>
              ))}
            </div>
            <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
              <li className="flex items-center gap-1.5">
                <CircleCheck className="size-3.5 text-success" /> فعال
              </li>
              <li className="flex items-center gap-1.5">
                <Ban className="size-3.5 text-danger" /> قفل
              </li>
              {other > 0 ? (
                <li className="flex items-center gap-1.5">
                  <CircleDashed className="size-3.5 text-fg-faint" /> دیگر وضعیت‌ها
                </li>
              ) : null}
            </ul>
            <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
              {[
                { l: "فعال", v: active },
                { l: "قفل", v: locked },
                { l: "دیگر", v: other },
              ].map((x) => (
                <div key={x.l} className="rounded-xl bg-surface-2 py-2">
                  <dt className="text-[0.7rem] text-fg-muted">{x.l}</dt>
                  <dd className="text-lg font-extrabold text-fg">{formatNumber(x.v)}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </div>
    </Card>
  );
}

// --- fleet plan -----------------------------------------------------------------------------------

function PlanCard() {
  const plan = useFleetPlan();
  const actions = plan.data?.actions ?? [];
  const blocked = actions.filter((a) => !a.allowed).length;
  return (
    <Card className="lg:col-span-8">
      <CardHeader
        icon={<Workflow className="size-5" />}
        title="برنامه‌ی چرخه‌ی ناوگان"
        subtitle={
          plan.data?.generated_at
            ? `آخرین برنامه ${formatRelative(plan.data.generated_at)} · ${formatJalali(plan.data.generated_at, "short")}`
            : "کارهایی که چرخه انجام میداد، با دلیل‌های قفل ایمنی"
        }
        action={plan.data?.dry_run ? <Badge tone="warning" dot>خشک</Badge> : null}
      />
      <div className="p-3 sm:p-4">
        {plan.isPending ? (
          <SkeletonLines lines={4} className="p-2" />
        ) : plan.isError ? (
          <ErrorState error={plan.error} onRetry={() => void plan.refetch()} compact />
        ) : plan.data.generated_at === null ? (
          <EmptyState icon={<Workflow className="size-7" />} title="هنوز برنامه‌ای ساخته نشده" description="چرخه‌ی ناوگان پس از اجرای worker، برنامه‌ی خود را اینجا میگذارد." />
        ) : actions.length === 0 ? (
          <EmptyState icon={<CircleCheck className="size-7" />} title="کاری لازم نیست" description="ناوگان با هدف همخوان است." />
        ) : (
          <>
            <div className="mb-2 flex flex-wrap gap-2 px-2">
              <Badge tone="brand">{formatNumber(actions.length)} کار</Badge>
              {blocked > 0 ? <Badge tone="danger">{formatNumber(blocked)} رد با قفل ایمنی</Badge> : <Badge tone="success">همه مجاز</Badge>}
            </div>
            <ul className="flex flex-col divide-y divide-line">
              {groupPlan(actions).map((g, i) => {
                const kind = planActionInfo(g.action);
                const shown = g.targets.slice(0, 6);
                return (
                  <motion.li key={`${g.action}-${g.targets.join(",")}`} {...fadeUp(i)} className="flex items-start gap-3 px-2 py-3">
                    <span className={cn("mt-0.5 shrink-0 rounded-lg px-2 py-1 text-xs font-bold", TONE_SOFT[kind.tone])}>
                      {kind.label}
                      {g.targets.length > 1 ? ` ×${formatNumber(g.targets.length)}` : ""}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="ltr text-right font-mono text-[0.8rem] leading-6 font-semibold break-words text-fg">
                        {shown.join(", ")}
                        {g.targets.length > shown.length ? ` +${String(g.targets.length - shown.length)}` : ""}
                      </div>
                      {g.reasons.length > 0 ? (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {g.reasons.map((r) => (
                            <span key={r} title={r} className="rounded-md bg-surface-3 px-1.5 py-0.5 text-[0.72rem] text-fg-muted">
                              {reasonLabel(r)}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    {g.allowed ? (
                      <Badge tone="success" className="gap-1">
                        <CircleCheck className="size-3.5" /> مجاز
                      </Badge>
                    ) : (
                      <Badge tone="danger" className="gap-1">
                        <Ban className="size-3.5" /> رد
                      </Badge>
                    )}
                  </motion.li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </Card>
  );
}

// --- activity --------------------------------------------------------------------------------------

function ActivityCard() {
  const events = useRecentEvents(100);
  return (
    <Card className="lg:col-span-12">
      <CardHeader
        icon={<Activity className="size-5" />}
        title="فعالیت سیستم"
        subtitle="هر کاری که پنل و چرخه‌ها کرده‌اند، با دلیل"
        action={
          <Link to="/events" className="inline-flex items-center gap-1 text-sm font-bold text-brand hover:underline">
            همه‌ی رویدادها
            <ArrowLeft className="size-4" />
          </Link>
        }
      />
      <div className="p-4 sm:p-5">
        {events.isPending ? (
          <div className="grid gap-4 lg:grid-cols-12">
            <Skeleton className="h-48 lg:col-span-7" />
            <SkeletonLines lines={5} className="lg:col-span-5" />
          </div>
        ) : events.isError ? (
          <ErrorState error={events.error} onRetry={() => void events.refetch()} compact />
        ) : events.data.length === 0 ? (
          <EmptyState icon={<Activity className="size-7" />} title="هنوز رویدادی ثبت نشده" description="با اولین کار پنل یا چرخه‌ها، اینجا پر میشود." />
        ) : (
          <div className="grid gap-5 lg:grid-cols-12">
            <div className="lg:col-span-7">
              <div className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <span className="text-sm font-semibold whitespace-nowrap text-fg">۱۴ روز اخیر</span>
                <ActivityLegend />
              </div>
              <ActivityChart events={events.data} days={14} className="h-52" />
            </div>
            <ul className="flex flex-col lg:col-span-5">
              {events.data.slice(0, 5).map((e) => (
                <EventRow key={e.id} event={e} compact />
              ))}
            </ul>
          </div>
        )}
      </div>
    </Card>
  );
}

// --- page -------------------------------------------------------------------------------------------

export default function OverviewPage() {
  const qc = useQueryClient();
  const ov = useOverview();
  const o = ov.data;
  const refreshing = ov.isFetching;

  return (
    <>
      <PageHeader
        eyebrow={formatJalali(new Date(), "long").replace(/، ساعت.*$/, "")}
        title="نمای کلی"
        subtitle={
          <span className="inline-flex items-center gap-2">
            <StatusDot tone={ov.isError ? "danger" : "success"} pulse={!ov.isError} />
            {ov.dataUpdatedAt ? `به‌روزرسانی خودکار · ${formatRelative(new Date(ov.dataUpdatedAt))}` : "در حال دریافت داده‌ی زنده"}
          </span>
        }
        actions={
          <IconButton
            label="به‌روزرسانی"
            onClick={() => void qc.invalidateQueries()}
            className="border border-line bg-surface shadow-card"
          >
            <RefreshCw className={cn("size-5", refreshing && "animate-spin")} />
          </IconButton>
        }
      />

      <div className="flex flex-col gap-4 sm:gap-5">
        <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
          <CostPerMemberHero overview={o} />
          {o ? (
            <KillSwitchStatus overview={o} />
          ) : (
            <Card className="p-5 lg:col-span-4">{ov.isError ? <ErrorState error={ov.error} compact onRetry={() => void ov.refetch()} /> : <SkeletonLines lines={5} />}</Card>
          )}
        </div>

        {o ? (
          <>
            <KpiRow o={o} />
            <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
              <SlotGrid o={o} />
              <BudgetCard o={o} />
              <Readiness o={o} />
            </div>
            <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
              <AccountsSplit o={o} />
              <PlanCard />
            </div>
          </>
        ) : ov.isError ? (
          <Card>
            <ErrorState error={ov.error} title="وضعیت ناوگان بارگذاری نشد" onRetry={() => void ov.refetch()} />
          </Card>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-32 rounded-3xl" />
              ))}
            </div>
            <div className="grid gap-4 lg:grid-cols-12">
              <Skeleton className="h-72 rounded-3xl lg:col-span-5" />
              <Skeleton className="h-72 rounded-3xl lg:col-span-4" />
              <Skeleton className="h-72 rounded-3xl lg:col-span-3" />
            </div>
          </>
        )}

        <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
          <ActivityCard />
        </div>
      </div>
    </>
  );
}
