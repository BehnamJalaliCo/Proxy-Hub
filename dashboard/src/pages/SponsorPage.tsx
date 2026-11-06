import { useQueryClient } from "@tanstack/react-query";
import {
  BadgeDollarSign,
  CalendarClock,
  CircleAlert,
  ExternalLink,
  Megaphone,
  MessagesSquare,
  Pencil,
  Plus,
  RotateCcw,
  Save,
  Settings2,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { motion } from "motion/react";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { BotListCard } from "../components/BotListCard";
import { Button, LinkButton } from "../components/ui/Button";
import { Badge, Card, CardHeader, StatusDot } from "../components/ui/Card";
import { ConfirmDialog, Modal } from "../components/ui/Dialogs";
import { EmptyState, LoadError, Skeleton } from "../components/ui/Feedback";
import { Field, TextInput, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import { formatJalali, formatNumber, formatRelative, toFaDigits } from "../lib/format";
import { nodeStateInfo } from "../lib/labels";
import {
  campaignBody,
  campaignStateInfo,
  canCancel,
  canDelete,
  canEdit,
  cancelCampaign,
  channelText,
  createCampaign,
  deleteCampaign,
  draftFromCampaign,
  emptyDraft,
  lastErrorText,
  leadText,
  moduleStatus,
  parseChannel,
  priceText,
  putSponsorSettings,
  regCounts,
  regStateInfo,
  retryRegistration,
  sortRegistrations,
  sponsorKey,
  sponsorLine,
  tagNote,
  updateCampaign,
  useSponsor,
  windowText,
  type CampaignDraft,
  type CampaignErrors,
  type SponsorCampaign,
  type SponsorOverview,
  type SponsorRegistration,
} from "../lib/sponsor";
import { toast } from "../lib/toast";
import { TONE_SOFT, TONE_TEXT } from "../lib/tones";
import { useNow } from "../lib/useNow";
import { asciiDigits } from "../lib/validation";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

/** Every write answers with the whole overview: put it in the cache, refresh the events. */
function useApply() {
  const qc = useQueryClient();
  return (o: SponsorOverview) => {
    qc.setQueryData(sponsorKey, o);
    void qc.invalidateQueries({ queryKey: ["events"] });
  };
}

// --- the module's state --------------------------------------------------------------------------------

function Tile({
  label,
  value,
  hint,
  tone = "neutral",
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "danger" | "success";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "min-w-0 rounded-2xl border p-3",
        className,
        tone === "danger" ? "border-danger/30 bg-danger-soft" : tone === "success" ? "border-success/30 bg-success-soft/40" : "border-line bg-surface-2",
      )}
    >
      <div className="text-xs text-fg-muted">{label}</div>
      <div className="mt-1 truncate text-xl font-extrabold text-fg">{value}</div>
      {hint ? <div className="mt-0.5 text-xs leading-5 text-fg-faint">{hint}</div> : null}
    </div>
  );
}

function StatusCard({ o, now }: { o: SponsorOverview; now: Date }) {
  const st = moduleStatus(o, now);
  const counts = regCounts(o.registrations);
  const campaign = o.campaigns.find((c) => c.id === o.active_campaign_id) ?? null;
  return (
    <Card className="overflow-hidden lg:col-span-12">
      <div className={cn("flex flex-col gap-3 p-5 sm:flex-row sm:items-start", TONE_SOFT[st.tone])}>
        <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-surface/70">
          {st.tone === "success" ? <ShieldCheck className={cn("size-6", TONE_TEXT[st.tone])} /> : <TriangleAlert className={cn("size-6", TONE_TEXT[st.tone])} />}
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="flex items-center gap-2 text-[1.05rem] leading-7 font-bold text-fg">
            <StatusDot tone={st.tone} pulse={st.tone === "success"} />
            {st.title}
          </h2>
          <p className="text-sm leading-7 text-fg-muted">{st.text}</p>
        </div>
        {st.link ? (
          <LinkButton to={st.link.to} variant="secondary" size="sm">
            {st.link.label}
          </LinkButton>
        ) : null}
      </div>
      <div className="grid grid-cols-2 gap-3 p-4 sm:p-5 lg:grid-cols-4">
        <Tile
          className="col-span-2 lg:col-span-1"
          label="اسپانسر خواسته‌شده"
          value={<span className="ltr inline-block">{channelText(o.wanted_channel)}</span>}
          hint={campaign ? <>کمپین <bdi>{campaign.customer}</bdi></> : "کانال پیش‌فرض"}
        />
        <Tile
          label="پروکسی‌های هماهنگ"
          value={`${formatNumber(counts.inSync)} از ${formatNumber(counts.live)}`}
          hint={counts.pending > 0 ? `${formatNumber(counts.pending)} در صف ثبت یا تنظیم` : "همه سر جای خود"}
          tone={counts.live > 0 && counts.inSync === counts.live ? "success" : "neutral"}
        />
        <Tile
          label="پیام به ربات در این ساعت"
          value={`${formatNumber(o.pacing.sent_last_hour)} از ${formatNumber(o.pacing.hourly_cap)}`}
          hint={`دست‌کم ${formatNumber(o.pacing.min_gap_s)} ثانیه بین هر دو پیام`}
        />
        <Tile
          label="ثبت‌های متوقف"
          value={formatNumber(counts.failed)}
          hint={`با ${formatNumber(o.pause_after_stuck)} توقف، همه‌ی گفتگوها می‌ایستد`}
          tone={counts.failed > 0 ? "danger" : "neutral"}
        />
      </div>
    </Card>
  );
}

// --- registrations -----------------------------------------------------------------------------------

function StuckBox({ r }: { r: SponsorRegistration }) {
  if (!r.stuck) return null;
  return (
    <div role="alert" className="flex flex-col gap-2 rounded-xl border border-danger/30 bg-danger-soft p-3 text-[0.84rem] leading-6 text-fg">
      <p className="flex items-start gap-2">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" />
        <span>
          <b className="text-danger">{r.stuck.reason_fa ?? r.stuck.reason}</b>
          {r.stuck.step_fa ? <span className="text-fg-muted"> · {r.stuck.step_fa}</span> : null}
          {r.stuck.at ? <span className="text-fg-muted"> · {formatRelative(r.stuck.at)}</span> : null}
        </span>
      </p>
      {r.stuck.hint_fa ? <p className="ps-6 text-fg">{r.stuck.hint_fa}</p> : null}
      {r.stuck.bot_text ? (
        <div className="ps-6">
          <div className="mb-1 text-xs font-semibold text-fg-muted">آخرین پاسخ ربات (secretها پوشانده شده‌اند):</div>
          <pre dir="ltr" className="max-h-48 overflow-auto rounded-lg bg-surface p-2.5 text-left font-mono text-xs leading-5 whitespace-pre-wrap text-fg">
            {r.stuck.bot_text}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function RegistrationRow({ r, index, now, onRetry }: { r: SponsorRegistration; index: number; now: Date; onRetry: () => void }) {
  const st = regStateInfo(r.state);
  const line = sponsorLine(r, now);
  const tag = tagNote(r);
  const node = r.node_state ? nodeStateInfo(r.node_state) : null;
  const err = r.state !== "failed" ? lastErrorText(r.last_error) : null;
  const retired = r.state === "retired";
  return (
    <motion.li
      {...fadeUp(index)}
      className={cn(
        "flex flex-col gap-2.5 rounded-2xl border p-3.5",
        r.state === "failed" ? "border-danger/30 bg-danger-soft/30" : r.in_sync ? "border-success/30 bg-success-soft/30" : "border-line bg-surface-2/50",
        retired && "opacity-70",
      )}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <div className="ltr truncate text-right font-mono text-sm font-bold text-fg" title={r.address}>
            {r.address}
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Badge tone={st.tone} dot pulse={r.state === "pending"}>
              {st.label}
            </Badge>
            {node ? <Badge tone={node.tone}>node: {node.label}</Badge> : null}
            {r.tag_hint ? (
              <Badge tone="brand">
                tag <span className="ltr inline-block font-mono">{r.tag_hint}</span>
              </Badge>
            ) : null}
          </div>
        </div>
        {!retired ? (
          <Button
            variant={r.state === "failed" ? "primary" : "secondary"}
            size="sm"
            className="self-start sm:self-center"
            icon={<RotateCcw className="size-4" />}
            onClick={onRetry}
          >
            ثبت دوباره
          </Button>
        ) : null}
      </div>
      <p className={cn("text-[0.84rem] leading-6", TONE_TEXT[line.tone])}>{line.text}</p>
      {tag ? <p className={cn("text-xs leading-6", TONE_TEXT.warning)}>{tag}</p> : null}
      {err ? (
        <p className="text-xs leading-6 text-fg-muted">
          {err}
          {r.next_attempt_at ? ` تلاش بعدی: ${formatRelative(r.next_attempt_at, now)}.` : null}
        </p>
      ) : null}
      <StuckBox r={r} />
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-faint">
        {r.registered_at ? <span title={formatJalali(r.registered_at)}>ثبت: {formatRelative(r.registered_at, now)}</span> : null}
        {r.promotion_set_at ? <span title={formatJalali(r.promotion_set_at)}>آخرین تنظیم اسپانسر: {formatRelative(r.promotion_set_at, now)}</span> : null}
        {retired && r.state_reason ? <span>دلیل: {r.state_reason === "node_gone" ? "node حذف شد" : r.state_reason === "address_changed" ? "نشانی عوض شد" : r.state_reason === "removed_in_bot" ? "در ‎@MTProxybot‎ پاک شده بود؛ دوباره ثبت شد" : r.state_reason}</span> : null}
      </div>
    </motion.li>
  );
}

function RegistrationsCard({ o, now }: { o: SponsorOverview; now: Date }) {
  const apply = useApply();
  const [target, setTarget] = useState<SponsorRegistration | null>(null);
  const [busy, setBusy] = useState(false);
  const regs = sortRegistrations(o.registrations);

  const retry = () => {
    if (!target) return;
    setBusy(true);
    retryRegistration(target.id)
      .then((next) => {
        apply(next);
        toast.success("ثبت دوباره در صف است", `«${target.address}» در گذر بعدی پنل (حداکثر یک دقیقه) دوباره انجام میشود.`);
        setTarget(null);
      })
      .catch((e: unknown) => toast.error("انجام نشد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <Card className="lg:col-span-7">
      <CardHeader
        icon={<MessagesSquare className="size-5" />}
        title="پروکسی‌ها در ‎@MTProxybot‎"
        subtitle="حساب عملیاتی (حساب دوم تلگرام) هر پروکسی زنده را ثبت میکند، tag را روی node میگذارد و «Set promotion» را میزند. تغییر اسپانسر حدود یک ساعت طول میکشد تا روی تلگرام دیده شود."
        action={<Badge tone="brand">{formatNumber(regs.filter((r) => r.state !== "retired").length)} ثبت</Badge>}
      />
      <div className="p-4 sm:p-5">
        {regs.length === 0 ? (
          <EmptyState
            icon={<Megaphone className="size-7" />}
            title="هنوز پروکسی‌ای ثبت نشده"
            description="با آماده شدن اولین node و پروکسی MTProto آن، پنل خودش آن را در ‎@MTProxybot‎ ثبت میکند و ‎@ipoxxo‎ را اسپانسرش میکند."
          />
        ) : (
          <ul className="flex flex-col gap-2.5">
            {regs.map((r, i) => (
              <RegistrationRow key={r.id} r={r} index={i} now={now} onRetry={() => setTarget(r)} />
            ))}
          </ul>
        )}
      </div>
      <ConfirmDialog
        open={target !== null}
        onOpenChange={(v) => {
          if (!v && !busy) setTarget(null);
        }}
        title={target ? `ثبت دوباره‌ی «${target.address}»` : ""}
        description={
          target?.has_tag
            ? "tag این پروکسی میماند و پنل یک بار دیگر «Set promotion» را با کانال خواسته‌شده میزند."
            : "پنل ثبت این پروکسی در ‎@MTProxybot‎ را از اول انجام میدهد (‎/newproxy‎). اگر ربات باز هم پاسخ ناآشنا بدهد، دوباره خبر میدهد."
        }
        confirmLabel="ثبت دوباره"
        icon={<RotateCcw className="size-6" />}
        busy={busy}
        onConfirm={retry}
      />
    </Card>
  );
}

// --- campaigns -----------------------------------------------------------------------------------------

function CampaignForm({
  initial,
  editing,
  defaultChannel,
  onDone,
}: {
  initial: CampaignDraft;
  editing: SponsorCampaign | null;
  defaultChannel: string;
  onDone: (o: SponsorOverview) => void;
}) {
  const [d, setD] = useState<CampaignDraft>(initial);
  const [errors, setErrors] = useState<CampaignErrors>({});
  const [busy, setBusy] = useState(false);
  const started = editing?.state === "active";
  const set = <K extends keyof CampaignDraft>(key: K, value: CampaignDraft[K]) => {
    setD((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => ({ ...prev, [key]: undefined }));
  };

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const checked = campaignBody(d, { defaultChannel, started });
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setBusy(true);
    const call = editing ? updateCampaign(editing.id, checked.body) : createCampaign(checked.body);
    call
      .then((o) => {
        const name = checked.body.channel.replace("https://t.me/", "");
        toast.success(editing ? "کمپین ویرایش شد" : "کمپین ساخته شد", `اسپانسر از ${formatJalali(checked.body.starts_at, "short")} به ${channelText(name)} میرود.`);
        onDone(o);
      })
      .catch((err: unknown) => toast.error("ذخیره نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  const preview = (value: string) => {
    const iso = value ? new Date(value) : null;
    return iso && !Number.isNaN(iso.getTime()) ? `به وقت تهران: ${formatJalali(iso, "long")}` : undefined;
  };

  return (
    <form onSubmit={submit} noValidate autoComplete="off" className="flex flex-col gap-4">
      <Field label="لینک کانال عمومی مشتری" htmlFor="cmp-channel" error={errors.channel} hint="فقط کانال عمومی: ‎https://t.me/name‎ یا ‎@name‎. لینک خصوصی (‎t.me/+…‎) پذیرفته نمیشود.">
        <TextInput id="cmp-channel" latin value={d.channel} onChange={(e) => set("channel", e.target.value)} invalid={Boolean(errors.channel)} placeholder="https://t.me/customer_channel" autoCapitalize="none" spellCheck={false} />
      </Field>
      <Field label="نام مشتری (اختیاری)" htmlFor="cmp-customer" error={errors.customer}>
        <TextInput id="cmp-customer" value={d.customer} onChange={(e) => set("customer", e.target.value)} invalid={Boolean(errors.customer)} placeholder="مثلاً فروشگاه نمونه" />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="شروع" htmlFor="cmp-starts" error={errors.starts} hint={started ? "کمپین شروع شده؛ زمان شروع دیگر عوض نمیشود." : preview(d.starts)}>
          <TextInput id="cmp-starts" type="datetime-local" latin value={d.starts} disabled={started} onChange={(e) => set("starts", e.target.value)} invalid={Boolean(errors.starts)} />
        </Field>
        <Field label="پایان" htmlFor="cmp-ends" error={errors.ends} hint={preview(d.ends)}>
          <TextInput id="cmp-ends" type="datetime-local" latin value={d.ends} onChange={(e) => set("ends", e.target.value)} invalid={Boolean(errors.ends)} />
        </Field>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_7rem] gap-3">
        <Field label="قیمت (اختیاری)" htmlFor="cmp-price" error={errors.price}>
          <TextInput id="cmp-price" latin inputMode="decimal" value={d.price} onChange={(e) => set("price", e.target.value)} invalid={Boolean(errors.price)} placeholder="120.00" />
        </Field>
        <Field label="واحد" htmlFor="cmp-currency" error={errors.currency}>
          <TextInput id="cmp-currency" latin value={d.currency} onChange={(e) => set("currency", e.target.value.toUpperCase())} invalid={Boolean(errors.currency)} placeholder="EUR" />
        </Field>
      </div>
      <Toggle checked={d.paid} onCheckedChange={(v) => set("paid", v)} label="پرداخت شده" description="فقط برای حساب‌وکتاب شماست؛ کمپین طبق زمان‌بندی اجرا میشود، پرداخت‌شده باشد یا نه." />
      <Button type="submit" loading={busy} icon={<Save className="size-5" />}>
        {editing ? "ذخیره‌ی تغییرات" : "ساخت کمپین"}
      </Button>
    </form>
  );
}

function CampaignRow({
  c,
  index,
  now,
  onEdit,
  onCancel,
  onDelete,
}: {
  c: SponsorCampaign;
  index: number;
  now: Date;
  onEdit: () => void;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const st = campaignStateInfo(c.state);
  return (
    <motion.li
      {...fadeUp(index)}
      className={cn("flex flex-col gap-2 rounded-2xl border p-3.5", c.effective ? "border-success/30 bg-success-soft/40" : "border-line bg-surface-2/50", (c.state === "finished" || c.state === "cancelled") && "opacity-75")}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-bold text-fg">
            <bdi>{c.customer}</bdi>
          </div>
          <a href={c.link} target="_blank" rel="noreferrer noopener" className="ltr inline-flex items-center gap-1 text-sm text-brand hover:underline">
            {channelText(c.channel)}
            <ExternalLink className="size-3.5" />
          </a>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
          <Badge tone={st.tone} dot pulse={c.effective}>
            {c.effective ? "اکنون اسپانسر" : st.label}
          </Badge>
          <Badge tone={c.paid ? "success" : "warning"}>{c.paid ? "پرداخت‌شده" : "پرداخت‌نشده"}</Badge>
        </div>
      </div>
      <div className="flex flex-col gap-0.5 text-[0.84rem] leading-6 text-fg-muted">
        <span className="flex items-center gap-1.5">
          <CalendarClock className="size-4 shrink-0" />
          {windowText(c)}
        </span>
        {c.state === "scheduled" ? (
          <span>
            تغییر اسپانسر از {formatJalali(c.switch_at, "short")} ({formatRelative(c.switch_at, now)}) شروع میشود.
          </span>
        ) : null}
        <span className="flex items-center gap-1.5">
          <BadgeDollarSign className="size-4 shrink-0" />
          {priceText(c.price, c.currency)}
        </span>
      </div>
      {canEdit(c) || canDelete(c, now) ? (
        <div className="flex flex-wrap gap-2">
          {canEdit(c) ? (
            <Button variant="secondary" size="sm" icon={<Pencil className="size-4" />} onClick={onEdit}>
              ویرایش
            </Button>
          ) : null}
          {canCancel(c) ? (
            <Button variant="secondary" size="sm" icon={<XCircle className="size-4" />} onClick={onCancel}>
              لغو
            </Button>
          ) : null}
          {canDelete(c, now) ? (
            <Button variant="ghost" size="sm" icon={<Trash2 className="size-4" />} onClick={onDelete}>
              حذف
            </Button>
          ) : null}
        </div>
      ) : null}
    </motion.li>
  );
}

type CampaignAction = { kind: "cancel" | "delete"; c: SponsorCampaign };

function CampaignsCard({ o, now, onNew, onEdit }: { o: SponsorOverview; now: Date; onNew: () => void; onEdit: (c: SponsorCampaign) => void }) {
  const apply = useApply();
  const [action, setAction] = useState<CampaignAction | null>(null);
  const [busy, setBusy] = useState(false);
  const open = o.campaigns.filter((c) => c.state === "scheduled" || c.state === "active");
  const closed = o.campaigns.filter((c) => !(c.state === "scheduled" || c.state === "active"));
  const list = [...open.sort((a, b) => a.starts_at.localeCompare(b.starts_at)), ...closed];

  const run = () => {
    if (!action) return;
    setBusy(true);
    const call = action.kind === "cancel" ? cancelCampaign(action.c.id) : deleteCampaign(action.c.id);
    call
      .then((next) => {
        apply(next);
        toast.success(action.kind === "cancel" ? "کمپین لغو شد" : "کمپین حذف شد", action.c.effective ? "اسپانسر در گذر بعدی پنل به کانال پیش‌فرض برمیگردد." : undefined);
        setAction(null);
      })
      .catch((e: unknown) => toast.error("انجام نشد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <Card className="lg:col-span-5">
      <CardHeader
        icon={<Megaphone className="size-5" />}
        title="کمپین‌های اسپانسر"
        subtitle={`در زمان کمپین، کانال مشتری «Proxy Sponsor» همه‌ی پروکسی‌ها میشود؛ تغییر ${leadText(o.settings.lead_minutes)} شروع میشود و در پایان، اسپانسر خودکار به ${channelText(o.settings.default_channel)} برمیگردد.`}
        action={
          <Button size="sm" icon={<Plus className="size-4" />} onClick={onNew}>
            کمپین تازه
          </Button>
        }
      />
      <div className="p-4 sm:p-5">
        {list.length === 0 ? (
          <EmptyState icon={<CalendarClock className="size-7" />} title="کمپینی نیست" description={`تا کمپینی نباشد، اسپانسر همه‌ی پروکسی‌ها ${channelText(o.settings.default_channel)} است.`} />
        ) : (
          <ul className="flex flex-col gap-2.5">
            {list.map((c, i) => (
              <CampaignRow key={c.id} c={c} index={i} now={now} onEdit={() => onEdit(c)} onCancel={() => setAction({ kind: "cancel", c })} onDelete={() => setAction({ kind: "delete", c })} />
            ))}
          </ul>
        )}
      </div>
      <ConfirmDialog
        open={action !== null}
        onOpenChange={(v) => {
          if (!v && !busy) setAction(null);
        }}
        title={action ? `${action.kind === "cancel" ? "لغو" : "حذف"} کمپین ${channelText(action.c.channel)}` : ""}
        description={
          action?.kind === "delete"
            ? "این کمپین هنوز شروع نشده و کامل پاک میشود."
            : "کمپین لغو میشود و در فهرست میماند. اگر اسپانسر روی کانال مشتری رفته باشد، پنل آن را به کانال پیش‌فرض برمیگرداند."
        }
        confirmLabel={action?.kind === "delete" ? "حذف کن" : "لغو کن"}
        tone="danger"
        icon={action?.kind === "delete" ? <Trash2 className="size-6" /> : <XCircle className="size-6" />}
        busy={busy}
        onConfirm={run}
      />
    </Card>
  );
}

// --- settings ------------------------------------------------------------------------------------------

function SettingsCard({ o }: { o: SponsorOverview }) {
  const apply = useApply();
  const [channel, setChannel] = useState(`@${o.settings.default_channel}`);
  const [lead, setLead] = useState(String(o.settings.lead_minutes));
  const [errors, setErrors] = useState<{ channel?: string; lead?: string }>({});
  const [busy, setBusy] = useState(false);

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const parsed = parseChannel(channel);
    const minutes = Number(asciiDigits(lead.trim()));
    const next: { channel?: string; lead?: string } = {};
    if (!parsed.ok) next.channel = parsed.error;
    if (!Number.isInteger(minutes) || minutes < 0 || minutes > 360) next.lead = "عددی بین ۰ تا ۳۶۰ دقیقه.";
    setErrors(next);
    if (!parsed.ok || next.lead) return;
    setBusy(true);
    putSponsorSettings({ default_channel: parsed.value, lead_minutes: minutes })
      .then((o2) => {
        apply(o2);
        toast.success("تنظیمات اسپانسر ذخیره شد", `کانال پیش‌فرض: ${channelText(o2.settings.default_channel)}`);
      })
      .catch((err: unknown) => toast.error("ذخیره نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <Card className="lg:col-span-12">
      <CardHeader icon={<Settings2 className="size-5" />} title="تنظیمات اسپانسر" subtitle="کانالی که بیرون از کمپین‌ها «Proxy Sponsor» همه‌ی پروکسی‌هاست، و چقدر زودتر از شروع کمپین تغییر آغاز شود." />
      <form onSubmit={submit} noValidate className="grid gap-4 p-5 sm:grid-cols-[1fr_12rem_auto] sm:items-start">
        <Field label="کانال پیش‌فرض اسپانسر" htmlFor="sp-default" error={errors.channel} hint="کانال باید عمومی باشد. پیش‌فرض: ‎@ipoxxo‎">
          <TextInput id="sp-default" latin value={channel} onChange={(e) => setChannel(e.target.value)} invalid={Boolean(errors.channel)} autoCapitalize="none" spellCheck={false} />
        </Field>
        <Field label="زودتر شروع شدن (دقیقه)" htmlFor="sp-lead" error={errors.lead} hint={`اکنون: ${leadText(o.settings.lead_minutes)}`}>
          <TextInput id="sp-lead" latin inputMode="numeric" value={lead} onChange={(e) => setLead(e.target.value)} invalid={Boolean(errors.lead)} />
        </Field>
        <Button type="submit" className="sm:mt-7" loading={busy} icon={<Save className="size-5" />}>
          ذخیره
        </Button>
      </form>
    </Card>
  );
}

// --- page ----------------------------------------------------------------------------------------------

export default function SponsorPage() {
  const q = useSponsor();
  const apply = useApply();
  const now = useNow(30_000);
  const o = q.data;
  const [form, setForm] = useState<{ editing: SponsorCampaign | null; key: number } | null>(null);

  return (
    <>
      <PageHeader
        title="اسپانسر"
        subtitle="ثبت خودکار پروکسی‌ها در ‎@MTProxybot‎ و کانال «Proxy Sponsor»: ‎@ipoxxo‎ به‌طور پیش‌فرض، کانال مشتری در زمان کمپین پولی."
        actions={
          o ? (
            <Button icon={<Plus className="size-5" />} onClick={() => setForm({ editing: null, key: Date.now() })}>
              کمپین تازه
            </Button>
          ) : undefined
        }
      />
      {q.isPending ? (
        <div className="grid gap-4 lg:grid-cols-12">
          <Skeleton className="h-48 rounded-3xl lg:col-span-12" />
          <Skeleton className="h-72 rounded-3xl lg:col-span-7" />
          <Skeleton className="h-72 rounded-3xl lg:col-span-5" />
        </div>
      ) : q.isError ? (
        <Card>
          <LoadError
            error={q.error}
            onRetry={() => void q.refetch()}
            notYet={{
              title: "بخش اسپانسر هنوز روی پنل فعال نشده",
              description: "ثبت خودکار پروکسی‌ها در ‎@MTProxybot‎ و کمپین‌های اسپانسر با به‌روزرسانی بعدی پنل فعال میشود. کاری از شما لازم نیست.",
            }}
          />
        </Card>
      ) : o ? (
        <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
          <StatusCard o={o} now={now} />
          <RegistrationsCard o={o} now={now} />
          <CampaignsCard o={o} now={now} onNew={() => setForm({ editing: null, key: Date.now() })} onEdit={(c) => setForm({ editing: c, key: Date.now() })} />
          {o.mtproxybot ? <BotListCard s={o.mtproxybot} onApply={apply} /> : null}
          <SettingsCard key={`${o.settings.default_channel}-${toFaDigits(o.settings.lead_minutes)}`} o={o} />
        </div>
      ) : null}
      {o ? (
        <Modal
          open={form !== null}
          onOpenChange={(v) => {
            if (!v) setForm(null);
          }}
          title={form?.editing ? `ویرایش کمپین ${channelText(form.editing.channel)}` : "کمپین اسپانسر تازه"}
          description="کانال مشتری در این بازه «Proxy Sponsor» همه‌ی پروکسی‌ها میشود."
          icon={<Megaphone className="size-6" />}
        >
          {form ? (
            <CampaignForm
              key={form.key}
              initial={form.editing ? draftFromCampaign(form.editing) : emptyDraft(now)}
              editing={form.editing}
              defaultChannel={o.settings.default_channel}
              onDone={(next) => {
                apply(next);
                setForm(null);
              }}
            />
          ) : null}
        </Modal>
      ) : null}
    </>
  );
}
