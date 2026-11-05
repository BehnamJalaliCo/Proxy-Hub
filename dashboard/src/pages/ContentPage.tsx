import { useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArchiveRestore,
  Bold,
  CalendarClock,
  CircleAlert,
  CircleCheck,
  Code,
  ExternalLink,
  EyeOff,
  FilePen,
  History,
  Italic,
  Library,
  Link2,
  Megaphone,
  PenLine,
  Plus,
  Radio,
  RefreshCw,
  Save,
  Send,
  Settings2,
  ShieldAlert,
  Sparkles,
  Strikethrough,
  Underline,
  Undo2,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Fragment, useMemo, useRef, useState, type ReactNode, type SubmitEvent } from "react";

import { OperatorPosterCard } from "../components/OperatorPosterCard";
import { Button, IconButton } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { ConfirmDialog, Drawer } from "../components/ui/Dialogs";
import { EmptyState, LoadError, Skeleton, SkeletonLines } from "../components/ui/Feedback";
import { Field, Segmented, TextInput, TimeSelect, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import { formatJalali, formatNumber, formatRelative, toFaDigits } from "../lib/format";
import {
  blockedText,
  contentStatusInfo,
  createContent,
  excerpt,
  LIMITS,
  LINK_STATUS,
  parseTgHtml,
  POST_STATE,
  postsThatFit,
  putPostingSettings,
  ROLE_LABEL,
  sameSettings,
  setContentStatus,
  toDraft,
  updateContent,
  validateContent,
  validateSettings,
  visibleLength,
  wrapSelection,
  type ContentItem,
  type ContentStatus,
  type PostingSettings,
  type PostsStatus,
  type RecentPost,
  type SettingsDraft,
  type TgNode,
} from "../lib/posts";
import { postsKeys, useContent, usePostingSettings, usePostPreview, usePostsStatus, useRecentPosts } from "../lib/postsQueries";
import { tehranMinutes } from "../lib/telegram";
import { toast } from "../lib/toast";
import { useNow } from "../lib/useNow";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.05 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

const NOT_YET = {
  title: "بخش محتوا و انتشار هنوز روی پنل فعال نشده",
  description: "پست خودکار، کتابخانه‌ی محتوا و پیش‌نمایش پست با به‌روزرسانی بعدی پنل فعال میشوند. کاری از شما لازم نیست.",
};

function Note({ tone, children }: { tone: "danger" | "warning" | "info" | "success"; children: ReactNode }) {
  const style = { danger: "bg-danger-soft text-danger", warning: "bg-warning-soft text-warning", info: "bg-info-soft text-info", success: "bg-success-soft text-success" }[tone];
  return (
    <motion.div
      role={tone === "danger" ? "alert" : undefined}
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className={cn("flex gap-2.5 rounded-2xl p-3.5 text-sm leading-7", style)}
    >
      <CircleAlert className="mt-1 size-4 shrink-0" />
      <div className="min-w-0 flex-1 text-fg">{children}</div>
    </motion.div>
  );
}

const clock = (iso: string) => formatJalali(iso, "time").slice(0, 5);

// --- a post as Telegram draws it -----------------------------------------------------------------------

function TgNodes({ nodes }: { nodes: TgNode[] }) {
  return (
    <>
      {nodes.map((n, i) => {
        if (n.type === "text") {
          // A block (quote, pre) already ends its line, as in Telegram: one newline after it is not a new line.
          const prev = nodes[i - 1];
          const afterBlock = prev?.type === "el" && (prev.tag === "blockquote" || prev.tag === "pre");
          return <Fragment key={i}>{afterBlock ? n.text.replace(/^\n/, "") : n.text}</Fragment>;
        }
        const inner = <TgNodes nodes={n.children} />;
        switch (n.tag) {
          case "b":
            return <b key={i} className="font-bold">{inner}</b>;
          case "i":
            return <i key={i}>{inner}</i>;
          case "u":
            return <u key={i}>{inner}</u>;
          case "s":
            return <s key={i} className="opacity-60">{inner}</s>;
          case "code":
            return (
              <code key={i} dir="ltr" className="inline-block max-w-full rounded-md bg-surface-3 px-1.5 py-0.5 font-mono text-[0.78rem] leading-5 break-all text-fg">
                {inner}
              </code>
            );
          case "pre":
            return (
              <pre key={i} dir="ltr" className="my-1 overflow-x-auto rounded-lg bg-surface-3 p-2 font-mono text-[0.78rem]">
                {inner}
              </pre>
            );
          case "blockquote":
            return (
              <blockquote key={i} className="my-1 block rounded-lg border-s-[3px] border-brand bg-brand-soft/50 px-3 py-1.5">
                {inner}
              </blockquote>
            );
          case "spoiler":
            return <span key={i} className="rounded bg-fg/70 text-transparent transition-colors hover:bg-transparent hover:text-fg">{inner}</span>;
          case "a":
            return n.href ? (
              <a key={i} href={n.href} target="_blank" rel="noopener noreferrer" className="font-semibold text-[#2a8bd6] underline-offset-2 hover:underline dark:text-[#6ab3f3]">
                {inner}
              </a>
            ) : (
              <span key={i}>{inner}</span>
            );
        }
        return null;
      })}
    </>
  );
}

/** A channel post bubble; the HTML comes from the panel and is drawn from React elements only. */
function TelegramBubble({ html, channel, at }: { html: string; channel: string; at?: string }) {
  const nodes = useMemo(() => parseTgHtml(html), [html]);
  return (
    <div className="rounded-3xl bg-[linear-gradient(160deg,color-mix(in_srgb,var(--ph-brand)_10%,var(--ph-surface-2)),var(--ph-surface-2))] p-3 sm:p-5">
      <div className="max-w-[34rem] rounded-2xl rounded-ee-md border border-line bg-surface px-3.5 pt-3 pb-2 shadow-card">
        <div dir="ltr" className="mb-1.5 text-right text-[0.82rem] font-bold text-brand">
          {channel}
        </div>
        {/* Each line takes the direction of its first letter, as Telegram draws a post. */}
        <div className="text-start text-[0.92rem] leading-7 break-words whitespace-pre-wrap text-fg [unicode-bidi:plaintext]">
          <TgNodes nodes={nodes} />
        </div>
        <div className="mt-1 text-left text-[0.7rem] text-fg-faint">{at ? clock(at) : "پیش‌نمایش"}</div>
      </div>
    </div>
  );
}

// --- state of the publisher ------------------------------------------------------------------------------

function DayDots({ s, now }: { s: PostsStatus; now: Date }) {
  const minute = tehranMinutes(now);
  return (
    <div className="pt-1 pb-5" aria-hidden>
      <div className="relative h-7 rounded-lg bg-surface-2 ring-1 ring-line">
        {s.today.planned.map((slot) => {
          const m = tehranMinutes(new Date(slot.at));
          const past = new Date(slot.at).getTime() <= now.getTime();
          return (
            <motion.span
              key={`${slot.day}-${String(slot.index)}`}
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              title={clock(slot.at)}
              className={cn("absolute top-1/2 size-3 -translate-y-1/2 rounded-full ring-2 ring-surface", past ? "bg-success" : "bg-brand/40 ring-brand")}
              style={{ insetInlineStart: `calc(${String((m / 1440) * 100)}% - 6px)` }}
            />
          );
        })}
        <div className="absolute inset-y-0 w-0.5 bg-fg" style={{ insetInlineStart: `${String((minute / 1440) * 100)}%` }} />
      </div>
      <div className="relative mt-1 h-4 text-[0.68rem] text-fg-faint">
        {[0, 6, 12, 18, 24].map((h) => (
          <span key={h} className="absolute -translate-x-1/2 rtl:translate-x-1/2" style={{ insetInlineStart: `${String((h / 24) * 100)}%` }}>
            {toFaDigits(String(h).padStart(2, "0"))}
          </span>
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: "success" | "warning" | "info" }) {
  const ring = tone === "success" ? "border-success/30 bg-success-soft" : tone === "warning" ? "border-warning/30 bg-warning-soft" : tone === "info" ? "border-info/30 bg-info-soft" : "border-line bg-surface-2";
  return (
    <div className={cn("min-w-0 rounded-2xl border p-3", ring)}>
      <div className="text-xs text-fg-muted">{label}</div>
      <div className="mt-1 truncate text-lg font-extrabold text-fg">{value}</div>
      {hint ? <div className="mt-0.5 truncate text-xs text-fg-muted">{hint}</div> : null}
    </div>
  );
}

function StatusCard({ s }: { s: PostsStatus }) {
  const now = useNow(30_000);
  const target = s.channels[s.target];
  const blocked = blockedText(s.blocked);
  const liveLinks = s.links.mtproto + s.links.reality;
  return (
    <Card>
      <CardHeader
        icon={<Megaphone className="size-5" />}
        title="انتشار خودکار"
        subtitle="محتوا، پروکسی‌های زنده و اسم کانال؛ پست‌ها حذف نمیشوند، ویرایش میشوند."
        action={
          s.target === "main" ? (
            <Badge tone="success" dot pulse>
              کانال اصلی
            </Badge>
          ) : (
            <Badge tone="info" dot>
              کانال آزمایشی
            </Badge>
          )
        }
      />
      <div className="flex flex-col gap-4 p-5">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="مقصد پست‌ها"
            value={ROLE_LABEL[s.target]}
            hint={target ? <span dir="ltr">{target}</span> : "ثبت نشده"}
            tone={s.target === "main" ? "success" : "info"}
          />
          <Stat label="پست‌های امروز" value={`${toFaDigits(s.today.posted)} از ${toFaDigits(s.today.planned.length)}`} hint={s.last_post_at ? `آخرین: ${formatRelative(s.last_post_at, now)}` : "هنوز پستی نرفته"} />
          <Stat
            label="پست بعدی"
            value={s.next_slot ? `ساعت ${clock(s.next_slot.at)}` : "—"}
            hint={s.next_slot ? formatRelative(s.next_slot.at, now) : "برنامه‌ای نیست"}
          />
          <Stat
            label="پروکسی و کانفیگ زنده"
            value={toFaDigits(liveLinks)}
            hint={`MTProto ${toFaDigits(s.links.mtproto)}، ریلیتی ${toFaDigits(s.links.reality)}`}
            tone={liveLinks > 0 ? "success" : "warning"}
          />
        </div>
        <DayDots s={s} now={now} />
        <AnimatePresence>
          {blocked ? <Note tone="warning">{blocked}</Note> : null}
          {s.quiet_now ? <Note tone="info">الان در بازه‌ی بی‌پست هستیم (صفحه‌ی تلگرام)؛ پست بعدی بعد از بازه میرود. ویرایش پست‌های قبلی ادامه دارد.</Note> : null}
          {!s.links.complete ? <Note tone="warning">یکی از منبع‌های پروکسی این بار جواب نداد؛ تا جواب ندهد هیچ پستی ویرایش نمیشود.</Note> : null}
          {s.links.providers === 0 ? (
            <Note tone="info">منبع پروکسی‌ها (MTProto و ریلیتی) هنوز به انتشار وصل نشده؛ با به‌روزرسانی بعدی پنل وصل میشود. تا آن وقت پستی نمیرود.</Note>
          ) : null}
        </AnimatePresence>
      </div>
    </Card>
  );
}

// --- settings ----------------------------------------------------------------------------------------------

function NumberField({ id, label, hint, suffix, value, error, onChange }: { id: string; label: string; hint: string; suffix: string; value: string; error: string | undefined; onChange: (v: string) => void }) {
  return (
    <Field label={label} htmlFor={id} hint={hint} error={error ?? null}>
      <TextInput id={id} inputMode="numeric" latin value={value} onChange={(e) => onChange(e.target.value)} invalid={Boolean(error)} suffix={suffix} className="pl-16" />
    </Field>
  );
}

function SettingsForm({ saved, s }: { saved: PostingSettings; s: PostsStatus }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<SettingsDraft>(() => toDraft(saved));
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmMain, setConfirmMain] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const { settings, errors } = validateSettings({ ...draft, enabled: saved.enabled, main_enabled: saved.main_enabled });
  // The switches save at once; the fields below save with the bar. Compare the fields only.
  const dirty = !settings || !sameSettings(settings, { ...saved });

  const write = (next: PostingSettings, done: string) => {
    setBusy(true);
    return putPostingSettings(next)
      .then((out) => {
        qc.setQueryData(postsKeys.settings, out);
        void qc.invalidateQueries({ queryKey: postsKeys.all });
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success(done);
        return out;
      })
      .catch((err: unknown) => {
        toast.error("ذخیره نشد", errorMessage(err));
        throw err;
      })
      .finally(() => setBusy(false));
  };

  const set = (patch: Partial<SettingsDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const save = (e?: SubmitEvent) => {
    e?.preventDefault();
    setSubmitted(true);
    if (!settings || busy) return;
    void write({ ...settings, enabled: saved.enabled, main_enabled: saved.main_enabled }, "تنظیمات انتشار ذخیره شد").then(
      (out) => {
        setDraft(toDraft(out));
        setSubmitted(false);
      },
      () => undefined,
    );
  };
  const mainSet = s.channels.main !== null;
  const fit = settings ? postsThatFit(settings.day_start, settings.day_end, s.post_interval_min) : null;

  return (
    <form onSubmit={save} noValidate className="flex flex-col gap-4 p-5">
      <div className="flex flex-col gap-4 rounded-2xl border border-line bg-surface-2/60 p-4">
        <Toggle
          checked={saved.enabled}
          disabled={busy}
          onCheckedChange={(v) => void write({ ...saved, enabled: v }, v ? "انتشار خودکار روشن شد" : "انتشار خودکار خاموش شد").catch(() => undefined)}
          label="انتشار خودکار"
          description="پست‌ها طبق برنامه‌ی روز، خودشان میروند. خاموش: هیچ پست تازه‌ای نمیرود (ویرایش پست‌های قبلی ادامه دارد)."
        />
        <div className="border-t border-line" />
        <Toggle
          checked={saved.main_enabled}
          disabled={busy || (!saved.main_enabled && !mainSet)}
          tone="warning"
          onCheckedChange={(v) => {
            if (v) setConfirmMain(true);
            else void write({ ...saved, main_enabled: false }, "پست‌ها از این پس به کانال آزمایشی میروند").catch(() => undefined);
          }}
          label="انتشار در کانال اصلی"
          description={
            mainSet ? (
              <>
                خاموش: همه‌ی پست‌ها فقط به کانال آزمایشی میروند. روشن: پست‌ها به <span dir="ltr">{s.channels.main}</span> میروند.
              </>
            ) : (
              "اول کانال اصلی را در صفحه‌ی تلگرام ثبت کنید."
            )
          }
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1">
        <NumberField id="pp-day" label="پست در روز" hint="۰ تا ۴۸؛ پیش‌فرض ۱۰، پخش در ساعت‌های پست." suffix="پست" value={draft.posts_per_day} error={errors.posts_per_day} onChange={(v) => set({ posts_per_day: v })} />
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-semibold text-fg">ساعت‌های پست (تهران)</span>
          <div className="flex flex-wrap items-center gap-2">
            <TimeSelect value={draft.day_start} onChange={(v) => set({ day_start: v })} label="آغاز ساعت‌های پست" invalid={Boolean(errors.day_start)} />
            <span className="text-sm text-fg-muted">تا</span>
            <TimeSelect value={draft.day_end} onChange={(v) => set({ day_end: v })} label="پایان ساعت‌های پست" invalid={Boolean(errors.day_end)} />
          </div>
          <p className={cn("text-[0.82rem] leading-6", errors.day_end ?? errors.day_start ? "text-danger" : "text-fg-faint")}>
            {errors.day_end ?? errors.day_start ?? "۰۰:۰۰ در پایان یعنی نیمه‌شب. بازه‌های بی‌پست صفحه‌ی تلگرام هم رعایت میشوند."}
          </p>
        </div>
      </div>
      {settings && fit !== null && settings.posts_per_day > fit ? (
        <div className="flex gap-3 rounded-2xl border border-warning/25 bg-warning-soft p-3.5 text-[0.84rem] leading-7 text-fg" role="status">
          <CircleAlert className="mt-1 size-5 shrink-0 text-warning" />
          <p>
            با فاصله‌ی دست‌کم {toFaDigits(s.post_interval_min)} دقیقه میان دو پست (صفحه‌ی تلگرام)، در این ساعت‌ها حدود {toFaDigits(fit)} پست جا میشود؛
            نوبت‌های بیشتر بی‌صدا جا میمانند. ساعت‌های پست را بازتر یا آن فاصله را کمتر کنید.
          </p>
        </div>
      ) : null}

      <button
        type="button"
        onClick={() => setAdvanced((v) => !v)}
        aria-expanded={advanced}
        className="flex items-center gap-2 self-start rounded-lg px-1 py-1 text-sm font-bold text-brand hover:underline"
      >
        <Settings2 className="size-4" />
        {advanced ? "بستن تنظیمات بیشتر" : "تنظیمات بیشتر"}
      </button>
      <AnimatePresence initial={false}>
        {advanced ? (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
            <div className="grid gap-4 sm:grid-cols-2">
              <NumberField id="pp-mt" label="پروکسی MTProto در هر پست" hint="۰ تا ۲۰" suffix="عدد" value={draft.max_mtproto} error={errors.max_mtproto} onChange={(v) => set({ max_mtproto: v })} />
              <NumberField id="pp-rl" label="کانفیگ ریلیتی در هر پست" hint="۰ تا ۵" suffix="عدد" value={draft.max_reality} error={errors.max_reality} onChange={(v) => set({ max_reality: v })} />
              <NumberField
                id="pp-window"
                label="ویرایش پست‌های اخیر تا"
                hint="پست‌های قدیمی‌تر دیگر ویرایش نمیشوند (۱ تا ۷۲۰ ساعت)."
                suffix="ساعت"
                value={draft.edit_window_hours}
                error={errors.edit_window_hours}
                onChange={(v) => set({ edit_window_hours: v })}
              />
              <NumberField
                id="pp-recycle"
                label="تکرار محتوای قبلی پس از"
                hint="وقتی محتوای تازه نماند (۱ تا ۳۶۵ روز)."
                suffix="روز"
                value={draft.recycle_after_days}
                error={errors.recycle_after_days}
                onChange={(v) => set({ recycle_after_days: v })}
              />
            </div>
            <div className="mt-4 flex flex-col gap-3 rounded-2xl border border-line bg-surface-2/60 p-4">
              <div className="text-sm font-bold text-fg">متن وضعیت کانفیگ تمام‌شده</div>
              {(["expired", "full", "burned"] as const).map((k) => (
                <Field key={k} htmlFor={`pp-st-${k}`} label={LINK_STATUS[k].label} error={errors[k] ?? null}>
                  <TextInput
                    id={`pp-st-${k}`}
                    value={draft.status_texts[k]}
                    maxLength={LIMITS.statusText + 20}
                    onChange={(e) => set({ status_texts: { ...draft.status_texts, [k]: e.target.value } })}
                    invalid={Boolean(errors[k])}
                  />
                </Field>
              ))}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {dirty ? (
          <motion.div
            initial={{ y: 80, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 80, opacity: 0 }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
            className="sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-20 lg:bottom-6"
          >
            <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface/95 p-3 shadow-pop backdrop-blur-xl">
              <span className="min-w-0 flex-1 text-sm font-semibold text-fg">
                {submitted && !settings ? <span className="text-danger">بخش‌های قرمز را اصلاح کنید.</span> : "تغییرات ذخیره نشده"}
              </span>
              <Button
                variant="ghost"
                size="sm"
                icon={<Undo2 className="size-4" />}
                disabled={busy}
                onClick={() => {
                  setDraft(toDraft(saved));
                  setSubmitted(false);
                }}
              >
                لغو
              </Button>
              <Button type="submit" size="sm" loading={busy} icon={<Save className="size-4" />}>
                ذخیره
              </Button>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <ConfirmDialog
        open={confirmMain}
        onOpenChange={setConfirmMain}
        title="انتشار در کانال اصلی روشن شود؟"
        description={
          <>
            از این پس پست‌های خودکار به جای کانال آزمایشی به <span dir="ltr">{s.channels.main}</span> میروند؛ همان {formatNumber(saved.posts_per_day)} پست در روز، در ساعت‌های پست و بیرون از بازه‌های بی‌پست.
          </>
        }
        acknowledge="پیش‌نمایش پست و چند پست کانال آزمایشی را دیده‌ام و قالب را تأیید میکنم."
        confirmLabel="روشن کن"
        tone="success"
        icon={<Radio className="size-6" />}
        busy={busy}
        onConfirm={() => {
          void write({ ...saved, main_enabled: true }, "پست‌ها از این پس به کانال اصلی میروند").then(
            () => setConfirmMain(false),
            () => undefined,
          );
        }}
      />
    </form>
  );
}

function SettingsCard({ s }: { s: PostsStatus }) {
  const settings = usePostingSettings();
  return (
    <Card className="lg:col-span-5">
      <CardHeader icon={<CalendarClock className="size-5" />} title="تنظیمات انتشار" subtitle="کمترین فاصله‌ی دو پست و بازه‌های بی‌پست در صفحه‌ی تلگرام است." />
      {settings.isPending ? (
        <div className="p-5">
          <SkeletonLines lines={5} />
        </div>
      ) : settings.isError ? (
        <LoadError error={settings.error} compact onRetry={() => void settings.refetch()} notYet={NOT_YET} />
      ) : (
        <SettingsForm saved={settings.data} s={s} />
      )}
    </Card>
  );
}

// --- preview -----------------------------------------------------------------------------------------------

function PreviewCard({ s }: { s: PostsStatus }) {
  const preview = usePostPreview();
  const p = preview.data;
  return (
    <Card className="lg:col-span-7">
      <CardHeader
        icon={<Sparkles className="size-5" />}
        title="پیش‌نمایش پست بعدی"
        subtitle="از داده‌ی واقعی همین لحظه ساخته شده: محتوای بعدی صف و پروکسی‌های زنده. چیزی فرستاده نمیشود."
        action={<IconButton label="ساختن دوباره" onClick={() => void preview.refetch()}><RefreshCw className={cn("size-4", preview.isFetching && "animate-spin")} /></IconButton>}
      />
      <div className="flex flex-col gap-3 p-4 sm:p-5">
        {preview.isPending ? (
          <Skeleton className="h-72 rounded-3xl" />
        ) : preview.isError ? (
          <LoadError error={preview.error} compact onRetry={() => void preview.refetch()} notYet={NOT_YET} />
        ) : p ? (
          <>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={p.target === "main" ? "success" : "info"}>{ROLE_LABEL[p.target]}</Badge>
              <Badge tone="neutral">{toFaDigits(p.chars)} از ۴٬۰۹۶ نویسه</Badge>
              <Badge tone={p.links.mtproto > 0 ? "brand" : "warning"}>MTProto {toFaDigits(p.links.mtproto)}</Badge>
              <Badge tone={p.links.reality > 0 ? "brand" : "neutral"}>ریلیتی {toFaDigits(p.links.reality)}</Badge>
              {p.content_item?.recycled ? <Badge tone="warning">محتوای تکراری</Badge> : null}
            </div>
            {p.blocked ? <Note tone="warning">{blockedText(p.blocked)}</Note> : null}
            <TelegramBubble html={p.text} channel={s.channel_tag} />
          </>
        ) : null}
      </div>
    </Card>
  );
}

// --- recent posts ------------------------------------------------------------------------------------------

function LinkChips({ post }: { post: RecentPost }) {
  return (
    <div className="flex flex-wrap gap-1">
      {post.links.map((l, i) => {
        const info = LINK_STATUS[l.status];
        return (
          <span
            key={`${l.kind}-${String(i)}`}
            title={info.label}
            className={cn(
              "inline-flex items-center gap-1 rounded-lg px-2 py-0.5 text-[0.72rem] font-semibold",
              l.status === "active" ? "bg-success-soft text-success" : "bg-surface-3 text-fg-muted line-through decoration-1",
            )}
          >
            {l.label}
            {info.mark ? <span className="no-underline">{info.mark}</span> : null}
          </span>
        );
      })}
    </div>
  );
}

function RecentCard() {
  const recent = useRecentPosts();
  const now = useNow(60_000);
  const posts = recent.data?.posts ?? [];
  return (
    <Card>
      <CardHeader icon={<History className="size-5" />} title="پست‌های اخیر" subtitle="هر پست با پروکسی‌هایی که دارد. پروکسی ازکارافتاده با ویرایش پست علامت میخورد؛ پست پاک نمیشود." />
      <div className="p-4 sm:p-5">
        {recent.isPending ? (
          <SkeletonLines lines={4} />
        ) : recent.isError ? (
          <LoadError error={recent.error} compact onRetry={() => void recent.refetch()} notYet={NOT_YET} />
        ) : posts.length === 0 ? (
          <EmptyState icon={<Send className="size-7 rtl:-scale-x-100" />} title="هنوز پست خودکاری نرفته" description="اولین پست در ساعت پست بعدی میرود، به شرط آنکه ربات، کانال و دست‌کم یک پروکسی زنده آماده باشد." />
        ) : (
          <ul className="flex flex-col gap-2.5">
            {posts.map((post, i) => {
              const state = POST_STATE[post.state];
              return (
                <motion.li key={post.id} {...fadeUp(Math.min(i, 8))} className="rounded-2xl border border-line bg-surface-2/60 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={state.tone} dot>
                      {state.label}
                    </Badge>
                    <Badge tone={post.role === "main" ? "success" : "neutral"}>{ROLE_LABEL[post.role]}</Badge>
                    {post.poster === "operator" ? (
                      <Badge tone="brand">
                        <Sparkles className="size-3" aria-hidden />
                        حساب دوم
                      </Badge>
                    ) : null}
                    <span className="text-xs text-fg-muted" title={formatJalali(post.created_at)}>
                      {formatRelative(post.created_at, now)}
                    </span>
                    {post.edits > 0 ? <Badge tone="warning">{toFaDigits(post.edits)} بار ویرایش شد</Badge> : null}
                    {post.edits_stopped ? <Badge tone="danger">ویرایش متوقف شد</Badge> : null}
                    {post.message_url ? (
                      <a href={post.message_url} target="_blank" rel="noopener noreferrer" className="ms-auto inline-flex items-center gap-1 text-xs font-bold text-brand hover:underline">
                        دیدن پست
                        <ExternalLink className="size-3.5" />
                      </a>
                    ) : null}
                  </div>
                  <div className="mt-2 truncate text-sm font-bold text-fg">{post.title ?? "بی محتوا (فقط پروکسی‌ها)"}</div>
                  <div className="mt-2">
                    <LinkChips post={post} />
                  </div>
                </motion.li>
              );
            })}
          </ul>
        )}
      </div>
    </Card>
  );
}

// --- content library -----------------------------------------------------------------------------------------

type Tab = ContentStatus | "all";

const TOOLBAR: { label: string; icon: ReactNode; open: string; close: string }[] = [
  { label: "پررنگ", icon: <Bold className="size-4" />, open: "<b>", close: "</b>" },
  { label: "کج", icon: <Italic className="size-4" />, open: "<i>", close: "</i>" },
  { label: "زیرخط", icon: <Underline className="size-4" />, open: "<u>", close: "</u>" },
  { label: "خط‌خورده", icon: <Strikethrough className="size-4" />, open: "<s>", close: "</s>" },
  { label: "پنهان (اسپویلر)", icon: <EyeOff className="size-4" />, open: "<tg-spoiler>", close: "</tg-spoiler>" },
  { label: "کد", icon: <Code className="size-4" />, open: "<code>", close: "</code>" },
];

function Editor({ item, onDone }: { item: ContentItem | null; onDone: () => void }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState(item?.title ?? "");
  const [body, setBody] = useState(item?.body ?? "");
  const [tags, setTags] = useState((item?.tags ?? []).join("، "));
  const [link, setLink] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const { input, errors } = validateContent(title, body, tags);
  const shownLength = visibleLength(body);

  const wrap = (open: string, close: string) => {
    const el = area.current;
    const out = wrapSelection(body, el?.selectionStart ?? body.length, el?.selectionEnd ?? body.length, open, close);
    setBody(out.text);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(out.start, out.end);
    });
  };

  const save = (status: "draft" | "approved" | null) => {
    setSubmitted(true);
    if (!input || busy) return;
    setBusy(true);
    const call = item ? updateContent(item.id, input).then((x) => (status === "approved" && x.status !== "approved" ? setContentStatus(x.id, "approved") : x)) : createContent(input, status ?? "draft");
    call
      .then((x) => {
        void qc.invalidateQueries({ queryKey: postsKeys.all });
        toast.success(item ? "محتوا ذخیره شد" : "محتوای تازه اضافه شد", contentStatusInfo(x.status).label);
        onDone();
      })
      .catch((err: unknown) => toast.error("ذخیره نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  const previewHtml = `<b>${title.replace(/</g, "&lt;")}</b>\n\n<blockquote>${body}</blockquote>`;

  return (
    <form
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        save(item ? null : "draft");
      }}
      className="flex flex-col gap-4"
    >
      <Field label="عنوان" htmlFor="ct-title" hint="یک خط کوتاه و گیرا؛ با یک ایموجی در اول بهتر دیده میشود." error={submitted ? (errors.title ?? null) : null}>
        <TextInput id="ct-title" value={title} maxLength={LIMITS.title + 20} onChange={(e) => setTitle(e.target.value)} invalid={submitted && Boolean(errors.title)} placeholder="💡 ترفند امروز" />
      </Field>
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <label htmlFor="ct-body" className="text-sm font-semibold text-fg">
            متن
          </label>
          <span className={cn("text-xs", shownLength > LIMITS.bodyVisible ? "font-bold text-danger" : "text-fg-faint")}>
            {toFaDigits(shownLength)} / {toFaDigits(LIMITS.bodyVisible)}
          </span>
        </div>
        <div className="flex flex-wrap gap-1 rounded-xl border border-line bg-surface-2 p-1">
          {TOOLBAR.map((t) => (
            <IconButton key={t.label} label={t.label} className="size-9" onClick={() => wrap(t.open, t.close)}>
              {t.icon}
            </IconButton>
          ))}
          <div className="flex min-w-40 flex-1 items-center gap-1">
            <input
              dir="ltr"
              value={link}
              onChange={(e) => setLink(e.target.value)}
              placeholder="https://…"
              aria-label="نشانی لینک"
              className="h-9 min-w-0 flex-1 rounded-lg border border-line bg-surface px-2 text-left text-sm text-fg outline-none focus:border-brand"
            />
            <IconButton
              label="لینک روی متن انتخاب‌شده"
              className="size-9"
              disabled={!/^https?:\/\/\S+$/i.test(link.trim())}
              onClick={() => {
                wrap(`<a href="${link.trim().replace(/"/g, "%22")}">`, "</a>");
                setLink("");
              }}
            >
              <Link2 className="size-4" />
            </IconButton>
          </div>
        </div>
        <textarea
          id="ct-body"
          ref={area}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={7}
          aria-invalid={(submitted && Boolean(errors.body)) || undefined}
          className={cn(
            "w-full rounded-xl border bg-surface-2 p-3.5 text-base leading-8 text-fg outline-none placeholder:text-fg-faint focus:border-brand focus:bg-surface focus:ring-4 focus:ring-[var(--ph-ring)]",
            submitted && errors.body ? "border-danger" : "border-line",
          )}
          placeholder="دو تا چهار خط کوتاه و کاربردی. برای قالب‌بندی، متن را انتخاب کنید و یکی از دکمه‌های بالا را بزنید."
        />
        {submitted && errors.body ? (
          <p role="alert" className="text-[0.82rem] leading-6 text-danger">
            {errors.body}
          </p>
        ) : (
          <p className="text-[0.82rem] leading-6 text-fg-faint">بدون سیاست، خبر تاریخ‌دار، ادعای پزشکی یا مالی. پنل برچسب‌های ناشناخته را برمیدارد.</p>
        )}
      </div>
      <Field label="برچسب‌ها" htmlFor="ct-tags" hint="با ویرگول یا فاصله جدا کنید؛ حداکثر ۸ برچسب." error={submitted ? (errors.tags ?? null) : null}>
        <TextInput id="ct-tags" value={tags} onChange={(e) => setTags(e.target.value)} invalid={submitted && Boolean(errors.tags)} placeholder="تلگرام، امنیت" />
      </Field>
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-semibold text-fg">پیش‌نمایش بالای پست</span>
        <TelegramBubble html={previewHtml} channel="@ipoxxo" />
      </div>
      <div className="sticky bottom-0 -mx-5 flex flex-wrap gap-2 border-t border-line bg-surface/95 px-5 py-3 backdrop-blur">
        {item ? (
          <>
            <Button type="submit" loading={busy} icon={<Save className="size-4" />}>
              ذخیره
            </Button>
            {item.status !== "approved" ? (
              <Button variant="success" loading={busy} icon={<CircleCheck className="size-4" />} onClick={() => save("approved")}>
                ذخیره و تأیید
              </Button>
            ) : null}
          </>
        ) : (
          <>
            <Button variant="success" loading={busy} icon={<CircleCheck className="size-4" />} onClick={() => save("approved")}>
              افزودن و تأیید
            </Button>
            <Button type="submit" variant="secondary" loading={busy} icon={<FilePen className="size-4" />}>
              ذخیره‌ی پیش‌نویس
            </Button>
          </>
        )}
      </div>
    </form>
  );
}

function ItemRow({ item, index, onEdit }: { item: ContentItem; index: number; onEdit: () => void }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const info = contentStatusInfo(item.status);
  const move = (status: "draft" | "approved" | "retired", done: string) => {
    setBusy(true);
    setContentStatus(item.id, status)
      .then(() => {
        void qc.invalidateQueries({ queryKey: postsKeys.all });
        toast.success(done);
      })
      .catch((err: unknown) => toast.error("انجام نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };
  return (
    <motion.li layout {...fadeUp(Math.min(index, 8))} className="rounded-2xl border border-line bg-surface-2/60 p-3.5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={info.tone}>{info.label}</Badge>
            {item.origin === "seed" ? <Badge tone="neutral">آماده‌ی پنل</Badge> : <Badge tone="brand">نوشته‌ی شما</Badge>}
            {item.used_count > 0 ? <span className="text-xs text-fg-muted">{toFaDigits(item.used_count)} بار در پست</span> : null}
          </div>
          <div className="mt-1.5 text-[0.95rem] leading-7 font-bold text-fg">{item.title}</div>
          <p className="mt-0.5 line-clamp-2 text-sm leading-7 text-fg-muted">{excerpt(item.body, 220)}</p>
          {item.tags.length > 0 ? (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {item.tags.map((t) => (
                <span key={t} className="rounded-md bg-surface-3 px-1.5 py-0.5 text-[0.72rem] text-fg-muted">
                  #{t}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <Button variant="secondary" size="sm" icon={<PenLine className="size-4" />} onClick={onEdit} disabled={busy}>
          ویرایش
        </Button>
        {item.status !== "approved" && item.status !== "used" ? (
          <Button variant="success" size="sm" icon={<CircleCheck className="size-4" />} loading={busy} onClick={() => move("approved", "تأیید شد و در صف پست رفت")}>
            تأیید
          </Button>
        ) : null}
        {item.status === "used" ? (
          <Button variant="secondary" size="sm" icon={<ArchiveRestore className="size-4" />} loading={busy} onClick={() => move("approved", "دوباره در صف پست رفت")}>
            دوباره در صف
          </Button>
        ) : null}
        {item.status === "approved" ? (
          <Button variant="ghost" size="sm" icon={<Undo2 className="size-4" />} loading={busy} onClick={() => move("draft", "به پیش‌نویس برگشت")}>
            پیش‌نویس
          </Button>
        ) : null}
        {item.status !== "retired" ? (
          <Button variant="ghost" size="sm" icon={<Archive className="size-4" />} loading={busy} onClick={() => move("retired", "بازنشسته شد؛ دیگر پست نمیشود")}>
            بازنشسته
          </Button>
        ) : (
          <Button variant="ghost" size="sm" icon={<ArchiveRestore className="size-4" />} loading={busy} onClick={() => move("draft", "به پیش‌نویس برگشت")}>
            برگرداندن
          </Button>
        )}
      </div>
    </motion.li>
  );
}

function LibraryCard() {
  const content = useContent();
  const [tab, setTab] = useState<Tab>("approved");
  const [editing, setEditing] = useState<ContentItem | "new" | null>(null);
  const data = content.data;
  const items = (data?.items ?? []).filter((i) => tab === "all" || i.status === tab);
  const count = (t: Tab) => (data ? (t === "all" ? data.items.length : data.counts[t]) : 0);
  const tabs: { value: Tab; label: string }[] = (["approved", "draft", "used", "retired", "all"] as const).map((t) => ({
    value: t,
    label: `${t === "all" ? "همه" : contentStatusInfo(t).label} ${toFaDigits(count(t))}`,
  }));
  return (
    <Card>
      <CardHeader
        icon={<Library className="size-5" />}
        title="کتابخانه‌ی محتوا"
        subtitle="بالای هر پست یکی از این‌ها میآید: اول تأییدشده‌های شما، بعد آماده‌های پنل، به ترتیب. هیچ محتوایی پاک نمیشود؛ بازنشسته میشود."
        action={
          <Button size="sm" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
            محتوای تازه
          </Button>
        }
      />
      <div className="flex flex-col gap-3 p-4 sm:p-5">
        {content.isPending ? (
          <SkeletonLines lines={6} />
        ) : content.isError ? (
          <LoadError error={content.error} compact onRetry={() => void content.refetch()} notYet={NOT_YET} />
        ) : (
          <>
            <div className="-mx-1 overflow-x-auto px-1 pb-1">
              <Segmented value={tab} onChange={setTab} options={tabs} label="وضعیت محتوا" className="min-w-max" />
            </div>
            {tab !== "all" ? <p className="text-xs leading-6 text-fg-muted">{contentStatusInfo(tab).hint}</p> : null}
            {data && data.counts.approved === 0 && tab === "approved" ? (
              <Note tone="warning">محتوای تأییدشده‌ی تازه‌ای نمانده؛ پست‌ها با محتوای قبلی (قدیمی‌ترین‌ها) میروند. محتوای تازه اضافه و تأیید کنید.</Note>
            ) : null}
            {items.length === 0 ? (
              <EmptyState icon={<ShieldAlert className="size-7" />} title="اینجا خالی است" description="محتوایی با این وضعیت نیست." />
            ) : (
              <ul className="grid gap-2.5 xl:grid-cols-2">
                {items.map((item, i) => (
                  <ItemRow key={item.id} item={item} index={i} onEdit={() => setEditing(item)} />
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <Drawer
        open={editing !== null}
        onOpenChange={(v) => {
          if (!v) setEditing(null);
        }}
        title={editing === "new" ? "محتوای تازه" : "ویرایش محتوا"}
        description="عنوان، متن و برچسب‌ها؛ پیش‌نمایش پایین همان چیزی است که بالای پست میآید."
        icon={<FilePen className="size-5" />}
      >
        {editing !== null ? <Editor key={editing === "new" ? "new" : editing.id} item={editing === "new" ? null : editing} onDone={() => setEditing(null)} /> : null}
      </Drawer>
    </Card>
  );
}

// --- the second account with Premium emoji (D38) ---------------------------------------------------------------

function OperatorSection() {
  const preview = usePostPreview(); // the same query as the preview card: fetched once
  return <OperatorPosterCard preview={preview.data} />;
}

// --- page -------------------------------------------------------------------------------------------------------

export default function ContentPage() {
  const status = usePostsStatus();
  const s = status.data;
  return (
    <>
      <PageHeader
        title="محتوا و انتشار"
        subtitle="پست‌های خودکار کانال: برنامه‌ی روز، پیش‌نمایش پست بعدی، پست‌های اخیر و کتابخانه‌ی محتوا. پست‌ها اول به کانال آزمایشی میروند تا شما کانال اصلی را روشن کنید."
      />
      {status.isPending ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-56 rounded-3xl" />
          <div className="grid gap-4 lg:grid-cols-12">
            <Skeleton className="h-96 rounded-3xl lg:col-span-5" />
            <Skeleton className="h-96 rounded-3xl lg:col-span-7" />
          </div>
        </div>
      ) : status.isError ? (
        <Card>
          <LoadError error={status.error} onRetry={() => void status.refetch()} notYet={NOT_YET} />
        </Card>
      ) : s ? (
        <div className="flex flex-col gap-4 sm:gap-5">
          <motion.div {...fadeUp(0)}>
            <StatusCard s={s} />
          </motion.div>
          <div className="grid items-start gap-4 sm:gap-5 lg:grid-cols-12">
            <SettingsCard s={s} />
            <PreviewCard s={s} />
          </div>
          <OperatorSection />
          <RecentCard />
          <LibraryCard />
        </div>
      ) : null}
    </>
  );
}
