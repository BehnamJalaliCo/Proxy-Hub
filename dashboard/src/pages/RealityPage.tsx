import { useQueryClient } from "@tanstack/react-query";
import { Clock3, Globe, HardDrive, Link2, LockKeyhole, Save, ServerCog, ShieldCheck, SlidersHorizontal, Undo2, Zap } from "lucide-react";
import { motion } from "motion/react";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader, StatusDot } from "../components/ui/Card";
import { PurgeEndedButton } from "../components/PurgeEnded";
import { EndedSection } from "../components/ui/EndedSection";
import { AnimatedNumber, CopyButton, EmptyState, LoadError, Skeleton } from "../components/ui/Feedback";
import { Field, Segmented, TextInput, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage, isApiError } from "../lib/api";
import { cn } from "../lib/browser";
import { isEndedConfig, isEndedNode, splitEnded } from "../lib/ended";
import { formatDurationSeconds, formatJalali, formatNumber, formatRelative, toFaDigits } from "../lib/format";
import type { Tone } from "../lib/labels";
import {
  certificateState,
  draftFrom,
  formatBytes,
  freshEveryHours,
  meterTone,
  parseDraft,
  REALITY_STATUS,
  sameSettings,
  savedMessage,
  sortConfigs,
  TARGET_AUTO_HINT,
  TARGET_HINT,
  TRANSPORT,
  usage,
  wsBlocker,
  xhttpState,
  xrayState,
  type RealityCertificate,
  type RealityConfig,
  type RealityDraft,
  type RealityDraftErrors,
  type RealityNode,
  type RealitySettings,
  type RealitySettingsView,
  type RealitySummary,
  type TargetMode,
  type Transport,
} from "../lib/reality";
import { putRealitySettings, realityKey, useReality } from "../lib/realityApi";
import { toast } from "../lib/toast";
import { TONE_DOT } from "../lib/tones";
import { useNow } from "../lib/useNow";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

// --- summary tiles -------------------------------------------------------------------------------

function Stat({ icon, label, value, format, note, i }: { icon: ReactNode; label: string; value: number; format: (n: number) => string; note: string; i: number }) {
  return (
    <motion.div {...fadeUp(i)}>
      <Card className="h-full p-4 sm:p-5">
        <div className="flex items-center gap-2 text-sm text-fg-muted">
          <span className="grid size-8 place-items-center rounded-xl bg-brand-soft text-brand">{icon}</span>
          {label}
        </div>
        <div className="mt-3 text-2xl font-black text-fg sm:text-3xl">
          <AnimatedNumber value={value} format={format} />
        </div>
        <p className="mt-1 text-xs leading-5 text-fg-faint">{note}</p>
      </Card>
    </motion.div>
  );
}

function Summary({ s }: { s: RealitySummary }) {
  const t = s.totals;
  const every = freshEveryHours(s.settings);
  return (
    <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
      <Stat i={0} icon={<Zap className="size-4" />} label="کانفیگ فعال" value={t.active} format={(n) => formatNumber(Math.round(n))} note={`هر node یک کانفیگ تازه در هر ${formatNumber(every, 1)} ساعت`} />
      <Stat i={1} icon={<ServerCog className="size-4" />} label="nodeهای Xray روشن" value={t.nodes_up} format={(n) => formatNumber(Math.round(n))} note={`از ${formatNumber(t.nodes_enabled)} node که Xray دارند`} />
      <Stat i={2} icon={<HardDrive className="size-4" />} label="مصرف همه‌ی کانفیگ‌ها" value={t.used_bytes} format={formatBytes} note="جمع ترافیک؛ به هیچ کاربری وصل نیست" />
      <Stat
        i={3}
        icon={<Clock3 className="size-4" />}
        label="تمام‌شده‌ها"
        value={t.expired + t.full + t.burned}
        format={(n) => formatNumber(Math.round(n))}
        note={`زمان: ${formatNumber(t.expired)} · حجم: ${formatNumber(t.full)} · سوخته: ${formatNumber(t.burned)}`}
      />
    </div>
  );
}

// --- configs -------------------------------------------------------------------------------------

function Meter({ label, ratio, text }: { label: string; ratio: number; text: string }) {
  const tone: Tone = meterTone(ratio);
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-center justify-between gap-2 text-xs text-fg-muted">
        <span>{label}</span>
        <span className="num">{text}</span>
      </div>
      <div
        className="mt-1.5 h-2 overflow-hidden rounded-full bg-surface-3"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(ratio * 100)}
      >
        <motion.div
          className={cn("h-full rounded-full", TONE_DOT[tone])}
          initial={{ width: 0 }}
          animate={{ width: `${String(Math.round(ratio * 1000) / 10)}%` }}
          transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
        />
      </div>
    </div>
  );
}

function LinkRow({ label, listed, url, active }: { label: string; listed: boolean; url: string | null; active: boolean }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-surface-3/40 px-2.5 py-1.5">
      <span className="flex items-center gap-2 text-xs font-semibold text-fg-muted">
        <span className="ltr inline-block">{label}</span>
        {active && !listed ? <Badge tone="info">منتظر تأیید node</Badge> : null}
      </span>
      {url ? <CopyButton text={url} label={`کپی لینک ${label}`} /> : null}
    </div>
  );
}

function ConfigItem({ c, now }: { c: RealityConfig; now: Date }) {
  const st = REALITY_STATUS[c.status];
  const active = c.status === "active";
  const u = usage(c, now);
  const left = Math.max(0, (new Date(c.expires_at).getTime() - now.getTime()) / 1000);
  const xhttp = c.xhttp;
  return (
    <li className={cn("rounded-2xl border border-line p-3 sm:p-4", active ? "bg-surface-2/50" : "bg-surface-2/20 opacity-80")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="ltr truncate font-bold text-fg">{c.name}</span>
          <Badge tone={st.tone} dot>
            {st.label}
          </Badge>
          <Badge tone="neutral">{TRANSPORT[c.transport].short}</Badge>
          {xhttp ? <Badge tone="brand">+ XHTTP</Badge> : null}
          {active && !c.listed && !xhttp ? <Badge tone="info">منتظر تأیید node</Badge> : null}
        </div>
        {c.url && !xhttp ? <CopyButton text={c.url} label="کپی لینک" /> : null}
      </div>
      {xhttp && active ? (
        <div className="mt-2 flex flex-col gap-1.5">
          <LinkRow label="TCP" listed={c.listed} url={c.url} active={active} />
          <LinkRow label="XHTTP" listed={xhttp.listed} url={xhttp.url} active={active} />
        </div>
      ) : null}
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-muted">
        <span>
          {c.slot !== null ? `جایگاه ${formatNumber(c.slot)}` : "بی جایگاه"} · <span className="ltr inline-block">{c.node_name}</span>
        </span>
        <span className="ltr inline-block font-mono">{c.host}</span>
        <span title={formatJalali(c.created_at)}>ساخته {formatRelative(c.created_at, now)}</span>
        {!active && c.ended_at ? <span title={formatJalali(c.ended_at)}>تمام شد {formatRelative(c.ended_at, now)}</span> : null}
      </div>
      {active ? (
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:gap-5">
          <Meter label={`زمان مانده: ${formatDurationSeconds(left)}`} ratio={u.time} text={formatJalali(c.expires_at, "short")} />
          <Meter label={`حجم مانده: ${formatBytes(c.remaining_bytes)}`} ratio={u.volume} text={`${formatBytes(c.used_bytes)} از ${formatBytes(c.data_cap_bytes)}`} />
        </div>
      ) : (
        <p className="mt-2 text-xs text-fg-faint">
          {st.hint} مصرف: {formatBytes(c.used_bytes)}
        </p>
      )}
    </li>
  );
}

function ConfigsCard({ configs }: { configs: RealityConfig[] }) {
  const now = useNow(30_000);
  const { current, ended } = splitEnded(sortConfigs(configs), isEndedConfig);
  return (
    <Card className="lg:col-span-7">
      <CardHeader
        icon={<Link2 className="size-5" />}
        title="کانفیگ‌ها"
        subtitle="کانفیگ‌های فعال؛ آن‌هایی که در ۷ روز گذشته تمام شدند در «پایان‌یافته‌ها» هستند. پست‌ها فقط کانفیگی را میگیرند که node آن را تأیید کرده است."
      />
      <div className="flex flex-col gap-3 p-3 sm:p-5">
        {current.length === 0 ? (
          <EmptyState
            icon={<Link2 className="size-7" />}
            title={ended.length ? "کانفیگ فعالی نیست" : "هنوز کانفیگی ساخته نشده"}
            description="وقتی node آماده‌ای با عامل تازه (Xray) گزارش بدهد، پنل خودش کانفیگ میسازد. کاری از شما لازم نیست."
          />
        ) : (
          <ul className="flex flex-col gap-2.5">
            {current.map((c) => (
              <ConfigItem key={c.id} c={c} now={now} />
            ))}
          </ul>
        )}
        <EndedSection count={ended.length} hint="زمان یا حجمشان تمام شد یا سرورشان کنار رفت." action={<PurgeEndedButton />}>
          <ul className="flex flex-col gap-2.5">
            {ended.map((c) => (
              <ConfigItem key={c.id} c={c} now={now} />
            ))}
          </ul>
        </EndedSection>
      </div>
    </Card>
  );
}

// --- nodes ---------------------------------------------------------------------------------------

function CertificateLine({ cert }: { cert: RealityCertificate | null }) {
  const st = certificateState(cert);
  return (
    <div className="mb-3 rounded-2xl border border-line bg-surface-2/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <LockKeyhole className="size-4 text-brand" />
        <span className="text-sm font-semibold text-fg">گواهی TLS</span>
        <Badge tone={st.tone} dot>
          {st.label}
        </Badge>
      </div>
      {cert ? (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-muted">
          <span className="ltr inline-block font-mono">{cert.names.join(" ")}</span>
          {cert.not_after ? <span title={formatJalali(cert.not_after)}>اعتبار تا {formatJalali(cert.not_after, "short")}</span> : null}
          {cert.last_error ? <span className="ltr inline-block font-mono">{cert.last_error}</span> : null}
          {!cert.valid && cert.retry_at ? <span>تلاش دوباره {formatRelative(cert.retry_at)}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

function NodeItem({ n, settings, cert }: { n: RealityNode; settings: RealitySettings; cert: RealityCertificate | null }) {
  const transport = settings.transport;
  const ws = transport === "ws_tls";
  const x = xrayState(n.xray, transport);
  const xh = xhttpState(n, settings);
  const blocker = ws ? wsBlocker(n, cert) : null;
  return (
    <li className="rounded-2xl border border-line bg-surface-2/50 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <StatusDot tone={x.tone} pulse={x.tone === "success"} />
          <span className="ltr truncate font-semibold text-fg">{n.name}</span>
        </span>
        <Badge tone="neutral">{n.slot !== null ? `جایگاه ${formatNumber(n.slot)}` : "آزمایشی"}</Badge>
      </div>
      <p className="mt-1 text-sm text-fg">{x.label}</p>
      {xh ? (
        <p className="mt-1 flex items-center gap-1.5 text-xs text-fg-muted">
          <StatusDot tone={xh.tone} />
          {xh.label}
        </p>
      ) : null}
      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
        <Badge tone={ws ? "info" : "neutral"}>{TRANSPORT[transport].short}</Badge>
        <span className="flex min-w-0 items-center gap-1 text-fg-muted">
          <Globe className="size-3.5 shrink-0" />
          {n.dns_name ? (
            <span className={cn("ltr truncate font-mono", n.dns_own ? "text-fg" : "text-fg-faint line-through")}>{n.dns_name}</span>
          ) : (
            <span>بی نام دامنه</span>
          )}
        </span>
      </div>
      {blocker ? <p className="mt-1 text-xs leading-5 text-warning">{blocker}</p> : null}
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-muted">
        <span>{formatNumber(n.active_configs)} کانفیگ فعال</span>
        {n.xray?.version ? <span className="ltr inline-block">Xray {n.xray.version}</span> : null}
        {n.xray?.reported_at ? <span>گزارش {formatRelative(n.xray.reported_at)}</span> : null}
        {ws ? (
          n.ws_path ? <span className="ltr inline-block font-mono">{n.ws_path}</span> : null
        ) : n.target ? (
          <span className="ltr inline-block font-mono">{n.target}</span>
        ) : null}
      </div>
      {!ws && n.target_next ? (
        <p className="mt-1.5 text-xs leading-5 text-fg-muted">
          <Badge tone="info">سایت هدف تازه</Badge> <span className="ltr inline-block font-mono">{n.target_next}</span> پس از پایان کانفیگ‌های فعلی
        </p>
      ) : null}
    </li>
  );
}

function NodesCard({ nodes, settings, cert }: { nodes: RealityNode[]; settings: RealitySettings; cert: RealityCertificate | null }) {
  const transport = settings.transport;
  const ws = transport === "ws_tls";
  const { current, ended } = splitEnded(nodes, isEndedNode);
  return (
    <Card className="lg:col-span-5">
      <CardHeader
        icon={<ServerCog className="size-5" />}
        title="Xray روی nodeها"
        subtitle={
          ws
            ? `روش: ${TRANSPORT.ws_tls.label}. هر node با نام دامنه‌ی خودش و گواهی Let's Encrypt کار میکند و TLS خودش را میآزماید.`
            : "هر node وضعیت Xray و آزمون سایت هدف (TLS 1.3 و HTTP/2) را خودش گزارش میدهد."
        }
      />
      <div className="p-3 sm:p-5">
        {ws ? <CertificateLine cert={cert} /> : null}
        {current.length === 0 ? (
          <EmptyState icon={<ServerCog className="size-7" />} title="nodeی نیست" description="با ساخته شدن nodeها، وضعیت Xray هر کدام اینجا میآید." />
        ) : (
          <ul className="flex flex-col gap-2">
            {current.map((n) => (
              <NodeItem key={n.node_id} n={n} settings={settings} cert={cert} />
            ))}
          </ul>
        )}
        <EndedSection count={ended.length} hint="سوخته یا در حال کنار رفتن." className="mt-3" action={<PurgeEndedButton />}>
          <ul className="flex flex-col gap-2">
            {ended.map((n) => (
              <NodeItem key={n.node_id} n={n} settings={settings} cert={cert} />
            ))}
          </ul>
        </EndedSection>
      </div>
    </Card>
  );
}

// --- settings ------------------------------------------------------------------------------------

const DRAFT_KEYS = new Set<string>(["enabled", "transport", "xhttp_enabled", "renew", "target_mode", "configs_per_node", "cap_hours", "cap_gb", "target_host", "target_port", "link_name"]);

/** 422 {"fields": ["cap_gb", ...]} -> which form fields the server refused. */
function serverErrors(err: unknown): RealityDraftErrors {
  const out: RealityDraftErrors = {};
  if (!isApiError(err)) return out;
  const fields = err.body["fields"];
  if (!Array.isArray(fields)) return out;
  for (const f of fields) {
    if (typeof f === "string" && DRAFT_KEYS.has(f)) out[f as keyof RealityDraft] = "سرور این مقدار را نپذیرفت؛ محدوده‌ی مجاز را بررسی کنید.";
  }
  return out;
}

function SettingsCard({ initial, defaults }: { initial: RealitySettingsView; defaults: RealitySettingsView }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<RealityDraft>(() => draftFrom(initial));
  const [server, setServer] = useState<RealityDraftErrors>({});
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const { settings, errors } = parseDraft(draft);
  const dirty = settings === null || !sameSettings(settings, initial);
  const shown = (k: keyof RealityDraft) => server[k] ?? (submitted || draft[k] !== draftFrom(initial)[k] ? errors[k] : undefined);
  const set = <K extends keyof RealityDraft>(k: K, v: RealityDraft[K]) => {
    setDraft((d) => ({ ...d, [k]: v }));
    setServer((e) => Object.fromEntries(Object.entries(e).filter(([key]) => key !== k)));
  };

  const save = (e?: SubmitEvent) => {
    e?.preventDefault();
    setSubmitted(true);
    if (!settings || busy) return;
    setBusy(true);
    putRealitySettings(settings)
      .then((saved) => {
        qc.setQueryData<RealitySummary>(realityKey, (old) => (old ? { ...old, settings: saved } : old));
        void qc.invalidateQueries({ queryKey: realityKey });
        void qc.invalidateQueries({ queryKey: ["events"] });
        setDraft(draftFrom(saved));
        setSubmitted(false);
        toast.success("تنظیمات Reality ذخیره شد", savedMessage(initial, saved));
      })
      .catch((err: unknown) => {
        setServer(serverErrors(err));
        toast.error("تنظیمات ذخیره نشد", errorMessage(err));
      })
      .finally(() => setBusy(false));
  };

  return (
    <Card className="lg:col-span-12">
      <CardHeader
        icon={<SlidersHorizontal className="size-5" />}
        title="تنظیمات Reality"
        subtitle={`هر کانفیگ تا ${formatNumber(initial.cap_hours)} ساعت یا ${formatNumber(initial.cap_gb)} گیگ کار میکند، هر کدام زودتر برسد${initial.renew ? "، و اگر سالم باشد با همان لینک تمدید میشود" : ""}. node خودش هم این سقف‌ها را اجرا میکند، حتی اگر پنل در دسترس نباشد.`}
      />
      <form onSubmit={save} noValidate className="flex flex-col gap-5 p-5">
        <div className="flex flex-col gap-2">
          <span className="text-sm font-semibold text-fg">روش اتصال کانفیگ‌ها</span>
          <Segmented<Transport>
            label="روش اتصال کانفیگ‌ها"
            value={draft.transport}
            onChange={(v) => set("transport", v)}
            options={[
              { value: "ws_tls", label: TRANSPORT.ws_tls.label },
              { value: "reality", label: TRANSPORT.reality.label },
            ]}
            className="w-full max-w-2xl flex-wrap sm:flex-nowrap"
          />
          <p className="text-xs leading-6 text-fg-muted">{TRANSPORT[draft.transport].hint}</p>
          {draft.transport !== initial.transport ? (
            <p className="text-xs leading-6 text-warning">با ذخیره، کانفیگ‌های فعال روش دیگر کنار میروند (🚫 روی پست‌ها)؛ هر node فقط یک روش را روی پورت ۴۴۳ دارد.</p>
          ) : null}
        </div>
        {draft.transport === "reality" ? (
          <Toggle
            checked={draft.xhttp_enabled}
            onCheckedChange={(v) => set("xhttp_enabled", v)}
            label={`XHTTP روی پورت ${toFaDigits(initial.xhttp_port)}`}
            description="هر کانفیگ دو لینک میگیرد: «کانفیگ N (TCP)» و «کانفیگ N (XHTTP)»، با همان زمان و حجم. XHTTP همان کلید Reality را دارد و روی پورت جدا اجرا میشود؛ لینکش وقتی منتشر میشود که node بگوید آن را بالا آورده است."
          />
        ) : null}
        <Toggle
          checked={draft.enabled}
          onCheckedChange={(v) => set("enabled", v)}
          label="ساخت خودکار کانفیگ تازه"
          description="خاموش: کانفیگ تازه ساخته نمیشود؛ کانفیگ‌های فعال تا پایان زمان یا حجمشان کار میکنند."
        />
        <Toggle
          checked={draft.renew}
          onCheckedChange={(v) => set("renew", v)}
          label="تمدید خودکار کانفیگ سالم"
          description="کانفیگی که هنوز کار میکند، یک ساعت پیش از پایانش با همان لینک دوباره به همین اندازه زمان و حجم میگیرد؛ کسی که آن را دارد قطع نمیشود و در پست‌های تازه هم دوباره می‌آید."
        />
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="سقف زمان هر کانفیگ" htmlFor="r-hours" hint={`ساعت؛ پیش‌فرض ${formatNumber(defaults.cap_hours)}.`} error={shown("cap_hours")}>
            <TextInput id="r-hours" inputMode="numeric" latin value={draft.cap_hours} onChange={(e) => set("cap_hours", e.target.value)} invalid={Boolean(shown("cap_hours"))} />
          </Field>
          <Field label="سقف حجم هر کانفیگ" htmlFor="r-gb" hint={`گیگابایت؛ پیش‌فرض ${formatNumber(defaults.cap_gb)}.`} error={shown("cap_gb")}>
            <TextInput id="r-gb" inputMode="numeric" latin value={draft.cap_gb} onChange={(e) => set("cap_gb", e.target.value)} invalid={Boolean(shown("cap_gb"))} suffix="GB" />
          </Field>
          <Field
            label="کانفیگ فعال برای هر node"
            htmlFor="r-per"
            hint={settings ? `یعنی یک کانفیگ تازه هر ${formatNumber(freshEveryHours(settings), 1)} ساعت در هر node.` : "۱ تا ۲۰."}
            error={shown("configs_per_node")}
          >
            <TextInput id="r-per" inputMode="numeric" latin value={draft.configs_per_node} onChange={(e) => set("configs_per_node", e.target.value)} invalid={Boolean(shown("configs_per_node"))} />
          </Field>
          {draft.transport === "reality" ? (
            <>
              <div className="flex flex-col gap-2 sm:col-span-2 lg:col-span-3">
                <span className="text-sm font-semibold text-fg">انتخاب سایت هدف</span>
                <Segmented<TargetMode>
                  label="انتخاب سایت هدف"
                  value={draft.target_mode}
                  onChange={(v) => set("target_mode", v)}
                  options={[
                    { value: "auto", label: "خودکار (از سنجش ایران)" },
                    { value: "manual", label: "دستی" },
                  ]}
                  className="w-full max-w-md"
                />
              </div>
              <Field
                label="سایت هدف (target)"
                htmlFor="r-target"
                hint={draft.target_mode === "auto" ? TARGET_AUTO_HINT : TARGET_HINT}
                error={shown("target_host")}
              >
                <TextInput
                  id="r-target"
                  latin
                  autoComplete="off"
                  value={draft.target_host}
                  onChange={(e) => set("target_host", e.target.value)}
                  invalid={Boolean(shown("target_host"))}
                  placeholder={defaults.target_host}
                  readOnly={draft.target_mode === "auto"}
                  aria-readonly={draft.target_mode === "auto"}
                />
              </Field>
              <Field label="پورت سایت هدف" htmlFor="r-tport" hint="معمولاً ۴۴۳." error={shown("target_port")}>
                <TextInput
                  id="r-tport"
                  inputMode="numeric"
                  latin
                  value={draft.target_port}
                  onChange={(e) => set("target_port", e.target.value)}
                  invalid={Boolean(shown("target_port"))}
                  readOnly={draft.target_mode === "auto"}
                  aria-readonly={draft.target_mode === "auto"}
                />
              </Field>
            </>
          ) : null}
          <Field label="نام کانفیگ در برنامه‌ی کاربر" htmlFor="r-name" hint="خالی: نام کانال اصلی (مثلاً ipoxxo). پرچم و شماره پس از آن میآید." error={shown("link_name")}>
            <TextInput id="r-name" latin autoComplete="off" value={draft.link_name} onChange={(e) => set("link_name", e.target.value)} invalid={Boolean(shown("link_name"))} placeholder="ipoxxo" />
          </Field>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" loading={busy} disabled={!dirty} icon={<Save className="size-4" />}>
            ذخیره
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={!dirty || busy}
            icon={<Undo2 className="size-4" />}
            onClick={() => {
              setDraft(draftFrom(initial));
              setServer({});
              setSubmitted(false);
            }}
          >
            برگرداندن
          </Button>
          <span className="text-xs text-fg-faint">
            پورت Xray روی همه‌ی nodeها {toFaDigits(initial.port)} است و XHTTP {toFaDigits(initial.xhttp_port)} (ثابت).
          </span>
        </div>
      </form>
    </Card>
  );
}

// --- page ----------------------------------------------------------------------------------------

export default function RealityPage() {
  const q = useReality();
  const s = q.data;
  return (
    <>
      <PageHeader
        title="Reality"
        subtitle="کانفیگ‌های VLESS که پست‌ها منتشر میکنند (Reality روی TCP و XHTTP، یا WebSocket + TLS با دامنه‌ی خود هر node): هر کدام کوتاه‌عمر، با زمان و حجم محدود، و بی هیچ ردی از IP کاربر."
      />
      {q.isPending ? (
        <div className="grid gap-4 lg:grid-cols-12">
          <Skeleton className="h-28 rounded-3xl lg:col-span-12" />
          <Skeleton className="h-72 rounded-3xl lg:col-span-7" />
          <Skeleton className="h-72 rounded-3xl lg:col-span-5" />
        </div>
      ) : q.isError ? (
        <Card>
          <LoadError
            error={q.error}
            onRetry={() => void q.refetch()}
            notYet={{
              title: "بخش Reality هنوز روی پنل فعال نشده",
              description: "کانفیگ‌های VLESS Reality با به‌روزرسانی بعدی پنل و nodeها فعال میشوند. کاری از شما لازم نیست.",
            }}
          />
        </Card>
      ) : s ? (
        <div className="flex flex-col gap-4 sm:gap-5">
          <Summary s={s} />
          <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
            <ConfigsCard configs={s.configs} />
            <NodesCard nodes={s.nodes} settings={s.settings} cert={s.certificate} />
            <SettingsCard key={JSON.stringify(s.settings)} initial={s.settings} defaults={s.defaults} />
          </div>
          <p className="flex items-center gap-2 text-xs leading-6 text-fg-faint">
            <ShieldCheck className="size-4 shrink-0 text-success" />
            روی nodeها لاگ دسترسی Xray خاموش است و فقط مصرف هر کانفیگ شمرده میشود؛ هیچ IP کاربری جایی ثبت یا فرستاده نمیشود.
          </p>
        </div>
      ) : null}
    </>
  );
}
