import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  ArrowLeft,
  BellRing,
  CircleCheck,
  CircleX,
  Cpu,
  FlaskConical,
  Gauge,
  History,
  LayoutGrid,
  ListChecks,
  MapPin,
  PlugZap,
  Radar,
  RefreshCw,
  Repeat,
  Server,
  ShieldX,
  Wallet,
} from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { LocationReachTable } from "../components/LocationReachTable";
import { ProbeGrid } from "../components/ProbeGrid";
import { Button, IconButton } from "../components/ui/Button";
import { Badge, Card, CardHeader, StatusDot } from "../components/ui/Card";
import { ConfirmDialog, Drawer } from "../components/ui/Dialogs";
import { PurgeEndedButton } from "../components/PurgeEnded";
import { EndedSection } from "../components/ui/EndedSection";
import { CopyButton, EmptyState, LoadError, Skeleton, SkeletonLines } from "../components/ui/Feedback";
import { PageHeader } from "../components/ui/PageHeader";
import { errorList, errorMessage, isApiError } from "../lib/api";
import { cn } from "../lib/browser";
import { isEndedNode, splitEnded } from "../lib/ended";
import * as api from "../lib/endpoints";
import {
  modeInfo,
  planNote,
  slotCounts,
  slotStatusInfo,
  spareText,
  useFleetSlots,
  type FleetSlot,
  type FleetSlots,
  type SlotNode,
} from "../lib/fleet";
import { formatDecimal, formatJalali, formatNumber, formatRelative } from "../lib/format";
import { locationName, nodeEventLabel, nodeStateInfo, reasonLabel, verdictInfo } from "../lib/labels";
import { agentHealth, canRetire, countByState, eventView, isLiveNode, liveTestNode, sortEvents, sortNodes } from "../lib/nodes";
import { targetOf } from "../lib/probes";
import { keys, useFleetSettings, useNodeEvents, useNodes, useOverview, useProbeSummary } from "../lib/queries";
import { toast } from "../lib/toast";
import { TONE_SOFT, TONE_TEXT } from "../lib/tones";
import type { NodeInfo, Overview, ProbeTarget } from "../lib/types";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

const NODES_NOT_YET = {
  title: "فهرست nodeها هنوز روی پنل فعال نشده",
  description: "فهرست nodeها و ساخت node آزمایشی با به‌روزرسانی بعدی پنل فعال میشود. تا آن وقت هیچ سروری از این صفحه ساخته نمیشود.",
};

// --- refusal of a safety gate --------------------------------------------------------------------

/** 409 gate_refused: every reason in Persian (the raw code stays in the tooltip for diagnosis). */
function GateRefusal({ error }: { error: unknown }) {
  const reasons = errorList(error, "reasons");
  const ref = useRef<HTMLDivElement>(null);
  // In a long dialog on a phone the answer lands below the fold: bring it into view.
  useEffect(() => {
    ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [error]);
  return (
    <motion.div
      ref={ref}
      role="alert"
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-2xl border border-danger/25 bg-danger-soft p-3.5 text-sm leading-7 text-fg"
    >
      <p className="flex items-start gap-2 font-bold">
        <ShieldX className="mt-1 size-4 shrink-0 text-danger" />
        {errorMessage(error)}
      </p>
      {reasons.length > 0 ? (
        <ul className="mt-2 flex flex-col gap-1 ps-6">
          {reasons.map((r) => (
            <li key={r} className="flex items-start gap-2" title={r}>
              <CircleX className="mt-1.5 size-3.5 shrink-0 text-danger" />
              <span>{reasonLabel(r)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </motion.div>
  );
}

// --- create the test node --------------------------------------------------------------------------

/** What the gates will check, from the live overview (the server decides; this only prepares). */
function GatePreview({ o }: { o: Overview | undefined }) {
  if (!o) return null;
  const checks = [
    { ok: !o.kill_switch, label: "کلید توقف خاموش باشد" },
    { ok: o.budget_cap_eur !== null, label: "سقف بودجه‌ی ماه تعیین شده باشد" },
    { ok: o.node_cap_total !== null, label: "سقف تعداد node تعیین شده باشد" },
    { ok: o.accounts.active > 0, label: "یک حساب هتزنر فعال با نگهبان سبز باشد" },
  ];
  return (
    <ul className="flex flex-col gap-1 rounded-2xl border border-line bg-surface-2 p-3 text-[0.84rem]">
      {checks.map((c) => (
        <li key={c.label} className="flex items-center gap-2">
          {c.ok ? <CircleCheck className="size-4 shrink-0 text-success" /> : <CircleX className="size-4 shrink-0 text-danger" />}
          <span className={c.ok ? "text-fg" : "font-semibold text-danger"}>{c.label}</span>
        </li>
      ))}
      <li className="flex items-center gap-2 text-fg-muted">
        <ListChecks className="size-4 shrink-0 text-brand" />
        و در لحظه‌ی ساخت: خرج پیش‌بینی ماه با این سرور از سقف نگذرد، سقف ساخت در ساعت پر نباشد و قیمت زنده‌ی هتزنر تازه باشد.
      </li>
    </ul>
  );
}

function CreateTestNodeDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const qc = useQueryClient();
  const overview = useOverview();
  const settings = useFleetSettings();
  const [location, setLocation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const o = overview.data;

  const close = (v: boolean) => {
    if (busy) return;
    if (!v) setError(null);
    onOpenChange(v);
  };

  const submit = () => {
    setBusy(true);
    setError(null);
    api
      .requestTestNode(location || undefined)
      .then(() => {
        void qc.invalidateQueries({ queryKey: keys.nodes });
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("درخواست node آزمایشی ثبت شد", "چرخه‌ی nodeها آن را با همه‌ی قفل‌های ایمنی میسازد؛ وضعیتش در همین صفحه دیده میشود.");
        onOpenChange(false);
      })
      .catch((e: unknown) => {
        setError(e);
        if (isApiError(e) && e.code === "test_node_exists") void qc.invalidateQueries({ queryKey: keys.nodes });
      })
      .finally(() => setBusy(false));
  };

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={close}
      title="ساخت node آزمایشی"
      description="یک سرور واقعی در حساب هتزنر nodeها ساخته میشود تا کل زنجیره (سرور، عامل، Telemt) آزموده شود. فقط یک node آزمایشی در هر زمان ممکن است."
      confirmLabel="بساز"
      icon={<FlaskConical className="size-6" />}
      acknowledge="میدانم که این یک سرور واقعی میسازد و تا وقتی بازنشسته نشود هزینه دارد."
      busy={busy}
      onConfirm={submit}
    >
      <div className="flex flex-col gap-3">
        <div className="flex gap-3 rounded-2xl border border-warning/25 bg-warning-soft p-3.5 text-[0.84rem] leading-7 text-fg">
          <Wallet className="mt-1 size-5 shrink-0 text-warning" />
          <p>
            <b>هزینه:</b> کوچک‌ترین نوع سرور مجاز در تنظیمات، ساعتی حساب میشود (برای cpx02 حدود ۰٫۰۱ یورو در ساعت) تا وقتی آن را
            بازنشسته کنید. node آزمایشی که یک ساعت ناسالم بماند، خودکار بازنشسته میشود.
            {o ? (
              <>
                {" "}
                خرج پیش‌بینی ماه الان <b className="num">{formatDecimal(o.month_projected_eur)} €</b>
                {o.budget_cap_eur ? (
                  <>
                    {" "}
                    از سقف <b className="num">{formatDecimal(o.budget_cap_eur)} €</b>
                  </>
                ) : null}{" "}
                است.
              </>
            ) : null}
          </p>
        </div>
        <div>
          <p className="mb-1.5 text-sm font-semibold text-fg">همه‌ی قفل‌های ایمنی باید سبز باشند:</p>
          <GatePreview o={o} />
        </div>
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-semibold text-fg">location</span>
          <select
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            className="h-11 rounded-xl border border-line bg-surface-2 px-3 text-base text-fg outline-none focus:border-brand sm:text-sm"
          >
            <option value="">خودکار (از locationهای تنظیمات)</option>
            {(settings.data?.locations ?? []).map((l) => (
              <option key={l} value={l}>
                {l} · {locationName(l)}
              </option>
            ))}
          </select>
        </label>
        {error ? <GateRefusal error={error} /> : null}
      </div>
    </ConfirmDialog>
  );
}

// --- one node -----------------------------------------------------------------------------------------

function Fact({ label, children, latin = false }: { label: string; children: ReactNode; latin?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[0.72rem] text-fg-faint">{label}</dt>
      <dd className={cn("truncate text-sm font-semibold text-fg", latin && "ltr text-right")}>{children}</dd>
    </div>
  );
}

function NodeCard({
  node,
  index,
  probe,
  onEvents,
  onRetire,
}: {
  node: NodeInfo;
  index: number;
  probe: ProbeTarget | undefined;
  onEvents: () => void;
  onRetire: () => void;
}) {
  const st = nodeStateInfo(node.state);
  const health = agentHealth(node.agent);
  const verdict = probe ? verdictInfo(probe.verdict) : null;
  const deleted = !isLiveNode(node);
  return (
    <motion.li {...fadeUp(index)} layout>
      <Card className={cn("flex h-full flex-col gap-4 p-5", deleted && "opacity-70", st.tone === "danger" && "border-danger/30")}>
        <div className="flex items-start gap-3">
          <span className={cn("grid size-11 shrink-0 place-items-center rounded-2xl", TONE_SOFT[st.tone])}>
            {node.is_test ? <FlaskConical className="size-5" /> : <Server className="size-5" />}
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="ltr truncate text-right font-mono text-[0.95rem] font-bold text-fg" title={node.name}>
              {node.name}
            </h3>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {node.is_test ? <Badge tone="brand">آزمایشی</Badge> : null}
              <span className="text-xs text-fg-muted">{node.slot !== null ? `جایگاه ${formatNumber(node.slot)}` : "بی جایگاه"}</span>
            </div>
          </div>
          <Badge tone={st.tone} dot pulse={st.moving}>
            {st.label}
          </Badge>
        </div>
        {st.hint ? <p className="-mt-1 text-[0.82rem] leading-6 text-fg-muted">{st.hint}</p> : null}

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          <Fact label="location">
            <span className="inline-flex items-center gap-1">
              <MapPin className="size-3.5 shrink-0 text-brand" />
              <span className="truncate">{locationName(node.location)}</span>
            </span>
          </Fact>
          <Fact label="نوع سرور" latin>
            {node.server_type}
          </Fact>
          <Fact label="ساخته‌شده">
            <span title={formatJalali(node.created_at, "datetime")}>{formatRelative(node.created_at)}</span>
          </Fact>
          <Fact label="آماده از">{node.ready_at ? <span title={formatJalali(node.ready_at, "datetime")}>{formatRelative(node.ready_at)}</span> : "—"}</Fact>
          <div className="col-span-2 flex items-end justify-between gap-2">
            <Fact label="IP سرور (مال خود node)" latin>
              {node.ipv4 ?? "—"}
            </Fact>
            {node.ipv4 ? <CopyButton text={node.ipv4} /> : null}
          </div>
        </dl>

        <div className="rounded-2xl border border-line bg-surface-2/70 p-3">
          <div className="flex items-center justify-between gap-2 text-sm">
            <span className="flex items-center gap-1.5 font-semibold text-fg">
              <Cpu className="size-4 text-brand" />
              عامل node
            </span>
            <span className="flex items-center gap-1.5 text-xs font-bold text-fg">
              <StatusDot tone={health.tone} pulse={health.tone === "success"} />
              {health.label}
            </span>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-2 text-center">
            <div className="rounded-xl bg-surface px-1 py-2">
              <div className="text-[0.68rem] text-fg-muted">اتصال الان</div>
              <div className="num text-lg font-extrabold text-fg">{node.agent.connections === null ? "—" : formatNumber(node.agent.connections)}</div>
            </div>
            <div className="rounded-xl bg-surface px-1 py-2">
              <div className="text-[0.68rem] text-fg-muted">آخرین گزارش</div>
              <div className="truncate text-xs font-bold text-fg">{node.agent.last_seen_at ? formatRelative(node.agent.last_seen_at) : "—"}</div>
            </div>
            <div className="rounded-xl bg-surface px-1 py-2">
              <div className="text-[0.68rem] text-fg-muted">Telemt</div>
              <div className="ltr truncate text-center text-xs font-bold text-fg">{node.agent.telemt_version ?? "—"}</div>
            </div>
          </div>
        </div>

        {verdict ? (
          <div className="flex items-center justify-between gap-2 text-sm">
            <span className="text-fg-muted">از ایران</span>
            <Badge tone={verdict.tone} dot>
              {verdict.label}
            </Badge>
          </div>
        ) : null}

        <div className="mt-auto flex flex-wrap gap-2 border-t border-line pt-4">
          <Button variant="secondary" size="sm" icon={<History className="size-4" />} onClick={onEvents}>
            رویدادها
          </Button>
          {/* Slot nodes are replaced and retired by the fleet itself (the server refuses: fleet_managed). */}
          {node.is_test && canRetire(node) ? (
            <Button variant="ghost" size="sm" className="text-danger" icon={<Archive className="size-4" />} onClick={onRetire}>
              بازنشسته کردن
            </Button>
          ) : null}
        </div>
      </Card>
    </motion.li>
  );
}

// --- events drawer -------------------------------------------------------------------------------------

function NodeEventsDrawer({ node, onClose }: { node: NodeInfo | null; onClose: () => void }) {
  const events = useNodeEvents(node?.id ?? null);
  const list = sortEvents(events.data ?? []);
  return (
    <Drawer
      open={node !== null}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
      title={node ? <span className="ltr inline-block font-mono">{node.name}</span> : ""}
      description="رویدادهای این node، تازه‌ترین بالا. هیچ IP کاربری در آن‌ها نیست."
      icon={<History className="size-5" />}
    >
      {events.isPending ? (
        <SkeletonLines lines={6} />
      ) : events.isError ? (
        <LoadError error={events.error} compact onRetry={() => void events.refetch()} />
      ) : list.length === 0 ? (
        <EmptyState icon={<History className="size-7" />} title="هنوز رویدادی ثبت نشده" />
      ) : (
        <ol className="relative ms-2 border-s border-line">
          {list.map((e, i) => {
            const v = eventView(e);
            return (
              <motion.li key={`${e.at}-${String(i)}`} {...fadeUp(Math.min(i, 8))} className="relative ps-5 pb-5 last:pb-0">
                <span className="absolute top-2 -start-[5px] size-2.5 rounded-full bg-brand ring-4 ring-surface" aria-hidden />
                <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                  <span className="text-sm font-bold text-fg">{nodeEventLabel(e.kind)}</span>
                  <time className="text-xs text-fg-faint" dateTime={e.at} title={formatJalali(e.at, "datetime")}>
                    {formatRelative(e.at)}
                  </time>
                </div>
                {v.change ? (
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
                    <span className="rounded-md bg-surface-3 px-1.5 py-0.5 font-semibold text-fg-muted">{v.change.from ?? "—"}</span>
                    <ArrowLeft className="size-3.5 text-fg-faint" />
                    <span className="rounded-md bg-brand-soft px-1.5 py-0.5 font-semibold text-brand">{v.change.to ?? "—"}</span>
                  </div>
                ) : null}
                {v.reasons.length > 0 ? (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {v.reasons.map((r) => (
                      <span key={r} className="rounded-md bg-surface-3 px-1.5 py-0.5 text-[0.75rem] text-fg-muted">
                        {r}
                      </span>
                    ))}
                  </div>
                ) : null}
                {v.fields.length > 0 ? (
                  <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[0.78rem]">
                    {v.fields.map((f) => (
                      <div key={f.key} className="contents">
                        <dt className="text-fg-faint">{f.label}</dt>
                        <dd className="ltr min-w-0 truncate text-right text-fg" title={f.value}>
                          {f.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                <div className="mt-1 text-[0.72rem] text-fg-faint">{formatJalali(e.at, "datetime")}</div>
              </motion.li>
            );
          })}
        </ol>
      )}
    </Drawer>
  );
}

// --- the 9 slots + 1 spare (D14) -----------------------------------------------------------------------

function SlotNodeLine({ node, label }: { node: SlotNode; label?: string }) {
  const st = nodeStateInfo(node.state);
  const verdict = node.verdict ? verdictInfo(node.verdict) : null;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {label ? <span className="text-[0.72rem] font-semibold text-fg-faint">{label}</span> : null}
        <Badge tone={st.tone} dot pulse={st.moving}>
          {st.label}
        </Badge>
        {node.blocked ? (
          <Badge tone="danger">بسته از ایران</Badge>
        ) : verdict ? (
          <Badge tone={verdict.tone}>{verdict.label}</Badge>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.8rem] text-fg-muted">
        <span className="inline-flex items-center gap-1">
          <MapPin className="size-3.5 shrink-0 text-brand" />
          {locationName(node.location)}
        </span>
        <span className="ltr font-mono text-fg" title="IP سرور (مال خود node)">
          {node.ipv4 ?? "—"}
        </span>
      </div>
    </div>
  );
}

function SlotCard({ slot, mode, index }: { slot: FleetSlot; mode: string; index: number }) {
  const info = slotStatusInfo(slot.status);
  const shown = slot.node;
  const note = planNote(slot, mode);
  const moving = slot.status === "starting" || slot.status === "replacing" || slot.status === "settling";
  return (
    <motion.li {...fadeUp(Math.min(index, 10))} layout>
      <Card className={cn("flex h-full flex-col gap-3 p-4", info.tone === "danger" && "border-danger/30")}>
        <div className="flex items-start gap-3">
          <span className={cn("grid size-10 shrink-0 place-items-center rounded-2xl text-base font-extrabold", TONE_SOFT[info.tone])}>
            {formatNumber(slot.slot)}
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-[0.95rem] font-bold text-fg">جایگاه {formatNumber(slot.slot)}</h3>
            <p className="ltr truncate text-right font-mono text-[0.75rem] text-fg-muted" title={slot.dns_name ?? undefined}>
              {slot.dns_name ?? "بی دامنه‌ی فعال: لینک‌ها با IP"}
            </p>
          </div>
          <Badge tone={info.tone} dot pulse={moving}>
            {info.label}
          </Badge>
        </div>
        {shown ? <SlotNodeLine node={shown} /> : <p className="text-sm text-fg-muted">{info.hint}</p>}
        {slot.replacement ? (
          <div className="rounded-2xl border border-info/25 bg-info-soft/60 p-2.5">
            <SlotNodeLine node={slot.replacement} label="جایگزین در راه" />
          </div>
        ) : null}
        {slot.others.map((n) => (
          <div key={n.id} className="rounded-2xl border border-line bg-surface-2/70 p-2.5">
            <SlotNodeLine node={n} label="سرور دیگر جایگاه" />
          </div>
        ))}
        {slot.leaving.length > 0 ? (
          <ul className="flex flex-col gap-1 text-[0.78rem] text-fg-muted">
            {slot.leaving.map((n) => (
              <li key={n.id} className="flex items-center gap-1.5">
                <Archive className="size-3.5 shrink-0 text-warning" />
                <span className="ltr font-mono">{n.name}</span>
                <span>· {n.retire_reason ? reasonLabel(n.retire_reason) : nodeStateInfo(n.state).label}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {note ? <p className="mt-auto border-t border-line pt-2.5 text-[0.78rem] leading-6 text-fg-muted">{note}</p> : null}
      </Card>
    </motion.li>
  );
}

function FleetSlotsSection() {
  const q = useFleetSlots();
  const data = q.data;
  return (
    <Card>
      <CardHeader
        icon={<LayoutGrid className="size-5" />}
        title="جایگاه‌ها: ۹ + ۱ جای خالی"
        subtitle={
          data ? (
            <>
              {formatNumber(data.target_slots)} جایگاه و {formatNumber(data.spare_slots)} جای خالی برای جایگزینی ·{" "}
              {data.active_domain ? (
                <>
                  دامنه‌ی فعال <span className="ltr inline-block font-mono">{data.active_domain}</span>
                </>
              ) : (
                "بی دامنه‌ی فعال"
              )}
            </>
          ) : (
            "هر جایگاه یک نام ثابت دارد؛ اگر سروری سوخت، اول جایگزینش ساخته میشود و بعد قدیمی حذف میشود."
          )
        }
        action={
          data ? (
            <Badge tone={modeInfo(data.mode).tone} dot pulse={data.mode === "live"}>
              {modeInfo(data.mode).label}
            </Badge>
          ) : null
        }
      />
      <div className="flex flex-col gap-4 p-3 sm:p-5">
        {q.isError ? (
          <LoadError
            error={q.error}
            compact
            onRetry={() => void q.refetch()}
            notYet={{
              title: "جایگاه‌های ناوگان هنوز روی پنل فعال نشده",
              description: "نمای جایگاه‌ها با به‌روزرسانی بعدی پنل فعال میشود.",
            }}
          />
        ) : !data ? (
          <SkeletonLines lines={5} className="p-2" />
        ) : (
          <>
            <div className={cn("flex gap-3 rounded-2xl p-3.5 text-[0.84rem] leading-7", TONE_SOFT[modeInfo(data.mode).tone])}>
              <Gauge className="mt-1 size-5 shrink-0" />
              <p className="text-fg">{modeInfo(data.mode).hint}</p>
            </div>
            <SlotTiles data={data} />
            <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {data.slots.map((s, i) => (
                <SlotCard key={s.slot} slot={s} mode={data.mode} index={i} />
              ))}
            </ul>
            <div className="flex flex-col gap-2 rounded-2xl border border-line bg-surface-2/70 p-3.5 text-sm">
              <span className="flex items-center gap-1.5 font-semibold text-fg">
                <Repeat className="size-4 text-brand" />
                جای خالی (سرور دهم)
              </span>
              <span className="text-fg-muted">{spareText(data, formatNumber)}</span>
              {data.excess.length > 0 ? (
                <span className="text-fg-muted">
                  بیرون از جایگاه‌ها: {data.excess.map((n) => n.name).join("، ")}
                </span>
              ) : null}
            </div>
            {data.alerts.length > 0 ? (
              <ul className="flex flex-col gap-1.5 rounded-2xl border border-warning/25 bg-warning-soft p-3.5 text-sm" role="status">
                {data.alerts.map((a) => (
                  <li key={a.code} className="flex flex-wrap items-center gap-1.5 text-fg">
                    <BellRing className="size-4 shrink-0 text-warning" />
                    <span className="font-semibold">{reasonLabel(a.code)}</span>
                    {a.since ? <span className="text-xs text-fg-muted">از {formatRelative(a.since)}</span> : null}
                    {a.last_sent_at ? <span className="text-xs text-fg-muted">· به تلگرام شما خبر داده شد</span> : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </div>
    </Card>
  );
}

function SlotTiles({ data }: { data: FleetSlots }) {
  const c = slotCounts(data);
  const items = [
    { label: "سالم", value: c.healthy, tone: "success" as const },
    { label: "در حال ساخت یا جایگزینی", value: c.working, tone: "info" as const },
    { label: "ناسالم یا سوخته", value: c.trouble, tone: "danger" as const },
    { label: "IP سوخته", value: data.burned_ips, tone: "neutral" as const, sub: `${formatNumber(data.burned_prefixes)} رنج /24` },
  ];
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map((t) => (
        <div key={t.label} className="rounded-2xl bg-surface-2/70 px-3 py-2.5">
          <div className="text-[0.72rem] text-fg-muted">{t.label}</div>
          <div className={cn("num text-xl font-extrabold", t.value > 0 ? TONE_TEXT[t.tone] : "text-fg")}>{formatNumber(t.value)}</div>
          {t.sub ? <div className="text-[0.7rem] text-fg-faint">{t.sub}</div> : null}
        </div>
      ))}
    </div>
  );
}

// --- page ----------------------------------------------------------------------------------------------

function Tile({ icon, label, value, sub, index }: { icon: ReactNode; label: string; value: string; sub?: string; index: number }) {
  return (
    <motion.div {...fadeUp(index)}>
      <Card className="h-full p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.8rem] font-semibold text-fg-muted">{label}</span>
          <span className="grid size-8 place-items-center rounded-xl bg-brand-soft text-brand">{icon}</span>
        </div>
        <div className="num mt-2 text-[1.8rem] leading-none font-extrabold text-fg">{value}</div>
        {sub ? <div className="mt-1.5 text-xs text-fg-muted">{sub}</div> : null}
      </Card>
    </motion.div>
  );
}

export default function NodesPage() {
  const qc = useQueryClient();
  const nodes = useNodes();
  const probes = useProbeSummary();
  const [creating, setCreating] = useState(false);
  const [retire, setRetire] = useState<NodeInfo | null>(null);
  const [retireError, setRetireError] = useState<unknown>(null);
  const [retiring, setRetiring] = useState(false);
  const [eventsOf, setEventsOf] = useState<NodeInfo | null>(null);

  const list = sortNodes(nodes.data ?? []);
  const split = splitEnded(list, isEndedNode);
  const live = list.filter(isLiveNode);
  const counts = countByState(list);
  const testNode = liveTestNode(list);
  const withConns = live.filter((n) => n.agent.connections !== null);
  const connections = withConns.reduce((s, n) => s + (n.agent.connections ?? 0), 0);
  const unhealthy = (counts.get("unhealthy") ?? 0) + (counts.get("burned") ?? 0);

  const doRetire = () => {
    if (!retire) return;
    setRetiring(true);
    setRetireError(null);
    api
      .retireNode(retire.id)
      .then(() => {
        void qc.invalidateQueries({ queryKey: keys.nodes });
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("بازنشستگی node شروع شد", "سرور پس از قفل‌های حذف، در هتزنر حذف میشود.");
        setRetire(null);
      })
      .catch((e: unknown) => {
        if (isApiError(e) && e.code === "not_found") {
          void qc.invalidateQueries({ queryKey: keys.nodes });
          toast.error("node پیدا نشد", errorMessage(e));
          setRetire(null);
        } else setRetireError(e);
      })
      .finally(() => setRetiring(false));
  };

  return (
    <>
      <PageHeader
        title="nodeها"
        subtitle="سرورهای پروکسی: وضعیت هر node، عامل آن، اتصال‌ها و باز بودن از ایران. هیچ IP کاربری اینجا نیست."
        actions={
          <Button
            icon={<FlaskConical className="size-5" />}
            onClick={() => setCreating(true)}
            disabled={!nodes.data || testNode !== undefined}
            title={testNode ? "یک node آزمایشی زنده هست؛ اول همان را بازنشسته کنید." : undefined}
          >
            ساخت node آزمایشی
          </Button>
        }
      />

      <div className="flex flex-col gap-4 sm:gap-5">
        <FleetSlotsSection />
        {nodes.isPending ? (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-28 rounded-3xl" />
              ))}
            </div>
            <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <li key={i}>
                  <Skeleton className="h-96 rounded-3xl" />
                </li>
              ))}
            </ul>
          </>
        ) : nodes.isError ? (
          <Card>
            <LoadError error={nodes.error} notYet={NODES_NOT_YET} onRetry={() => void nodes.refetch()} />
          </Card>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
              <Tile index={0} icon={<Server className="size-4" />} label="node زنده" value={formatNumber(live.length)} sub={testNode ? "یکی از آن‌ها آزمایشی است" : "بی node آزمایشی"} />
              <Tile index={1} icon={<CircleCheck className="size-4" />} label="آماده" value={formatNumber(counts.get("ready") ?? 0)} sub="سالم و در کار" />
              <Tile index={2} icon={<Activity className="size-4" />} label="اتصال الان" value={withConns.length ? formatNumber(connections) : "—"} sub="جمع گزارش عامل‌ها (فقط تعداد)" />
              <Tile index={3} icon={<PlugZap className="size-4" />} label="ناسالم یا سوخته" value={formatNumber(unhealthy)} sub={unhealthy ? "پنل خودش ترمیم یا جایگزین میکند" : "همه سالم‌اند"} />
            </div>

            {list.length === 0 ? (
              <Card>
                <EmptyState
                  icon={<Server className="size-7" />}
                  title="هنوز node‌ای ساخته نشده"
                  description="برای آزمودن کل زنجیره (ساخت سرور در هتزنر، ثبت عامل، اجرای Telemt) یک node آزمایشی بسازید. ساخت فقط وقتی انجام میشود که همه‌ی قفل‌های ایمنی سبز باشند."
                  action={
                    <Button icon={<FlaskConical className="size-5" />} onClick={() => setCreating(true)}>
                      ساخت node آزمایشی
                    </Button>
                  }
                />
              </Card>
            ) : (
              <>
                {split.current.length === 0 ? (
                  <Card>
                    <EmptyState
                      icon={<Server className="size-7" />}
                      title="node فعالی نیست"
                      description="همه‌ی nodeها کنار رفته یا حذف شده‌اند؛ پایین‌تر در «پایان‌یافته‌ها» هستند."
                    />
                  </Card>
                ) : (
                  <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {split.current.map((n, i) => (
                      <NodeCard
                        key={n.id}
                        node={n}
                        index={i}
                        probe={targetOf(probes.data?.targets, n.id)}
                        onEvents={() => setEventsOf(n)}
                        onRetire={() => {
                          setRetireError(null);
                          setRetire(n);
                        }}
                      />
                    ))}
                  </ul>
                )}
                <EndedSection
                  count={split.ended.length}
                  hint="سوخته، در حال کنار رفتن یا حذف‌شده (حذف‌شده‌ها تا یک هفته)."
                  action={<PurgeEndedButton />}
                >
                  <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                    {split.ended.map((n, i) => (
                      <NodeCard
                        key={n.id}
                        node={n}
                        index={i}
                        probe={targetOf(probes.data?.targets, n.id)}
                        onEvents={() => setEventsOf(n)}
                        onRetire={() => {
                          setRetireError(null);
                          setRetire(n);
                        }}
                      />
                    ))}
                  </ul>
                </EndedSection>
              </>
            )}
          </>
        )}

        <Card>
          <CardHeader
            icon={<Radar className="size-5" />}
            title="باز بودن از ایران"
            subtitle={
              probes.data?.last_run_at
                ? `آخرین سنجش ${formatRelative(probes.data.last_run_at)} · ${formatJalali(probes.data.last_run_at, "short")}`
                : "هر node از شبکه‌ی اپراتورهای ایران با دست‌دادن واقعی TLS (Globalping) سنجیده میشود."
            }
            action={
              probes.data ? (
                <IconButton label="به‌روزرسانی" onClick={() => void probes.refetch()} className="border border-line">
                  <RefreshCw className={cn("size-4", probes.isFetching && "animate-spin")} />
                </IconButton>
              ) : null
            }
          />
          <div className="p-3 sm:p-5">
            {probes.isPending ? (
              <SkeletonLines lines={4} className="p-2" />
            ) : probes.isError ? (
              <LoadError
                error={probes.error}
                compact
                onRetry={() => void probes.refetch()}
                notYet={{
                  title: "سنجش از ایران هنوز روی پنل فعال نشده",
                  description: "سنجش باز بودن هر node از شبکه‌های ایران (Globalping و check-host) با به‌روزرسانی بعدی پنل فعال میشود.",
                }}
              />
            ) : (
              <ProbeGrid summary={probes.data} />
            )}
          </div>
        </Card>

        {probes.data?.locations ? (
          <Card>
            <CardHeader
              icon={<MapPin className="size-5" />}
              title="locationها از ایران"
              subtitle={`هر location هتزنر از هر اپراتور، از روی دست‌دادن TLS با nodeهای ما در ${formatNumber(probes.data.locations.window_days)} روز اخیر.`}
            />
            <div className="p-3 sm:p-5">
              <LocationReachTable summary={probes.data.locations} />
            </div>
          </Card>
        ) : null}
      </div>

      <CreateTestNodeDialog open={creating} onOpenChange={setCreating} />

      <ConfirmDialog
        open={retire !== null}
        onOpenChange={(v) => {
          if (!v && !retiring) setRetire(null);
        }}
        title={retire ? `بازنشستگی «${retire.name}»` : ""}
        description="سرور این node در هتزنر حذف میشود. پنل پیش از حذف دوباره همه‌ی قفل‌های حذف را میسنجد: برچسب managed-by=proxy-hub روی سرور و ثبت آن در همین پنل. این کار برگشت ندارد."
        confirmLabel="بازنشسته کن"
        tone="danger"
        icon={<Archive className="size-6" />}
        busy={retiring}
        onConfirm={doRetire}
      >
        {retireError ? <GateRefusal error={retireError} /> : null}
      </ConfirmDialog>

      <NodeEventsDrawer node={eventsOf} onClose={() => setEventsOf(null)} />
    </>
  );
}
