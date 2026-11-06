import { useQueryClient } from "@tanstack/react-query";
import { Camera, ExternalLink, Inbox, Plus, RefreshCw, Rss, Send, Trash } from "lucide-react";
import { motion } from "motion/react";
import { useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { ConfirmDialog } from "../components/ui/Dialogs";
import { EmptyState, LoadError, SkeletonLines } from "../components/ui/Feedback";
import { Field, Segmented, TextInput, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage } from "../lib/api";
import {
  addSource,
  fetchSourceNow,
  KIND_LABEL,
  removeSource,
  sourceHealth,
  sourceInputError,
  updateSource,
  type ContentSource,
  type SourceKind,
} from "../lib/contentEngine";
import { contentKeys, useSources } from "../lib/contentEngineQueries";
import { formatRelative, toFaDigits } from "../lib/format";
import { toast } from "../lib/toast";

const NOT_YET = {
  title: "موتور محتوا هنوز روی پنل فعال نشده",
  description: "منبع‌های محتوا و صف بازبینی با به‌روزرسانی بعدی پنل فعال میشوند. کاری از شما لازم نیست.",
};

const PLACEHOLDER: Record<SourceKind, string> = {
  telegram: "@bbcpersian یا https://t.me/bbcpersian",
  rss: "https://example.com/rss.xml",
};

function AddSource({ full }: { full: boolean }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<SourceKind>("telegram");
  const [ref, setRef] = useState("");
  const [label, setLabel] = useState("");
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const problem = sourceInputError(kind, ref, label);
  const submit = (e: SubmitEvent<HTMLFormElement>) => {
    e.preventDefault();
    setTried(true);
    if (problem || full) return;
    setBusy(true);
    addSource({ kind, ref: ref.trim(), ...(label.trim() ? { label: label.trim() } : {}) })
      .then((s) => {
        toast.success("منبع اضافه شد", `«${s.label}» در دور بعد (حداکثر یک دقیقه) خوانده میشود.`);
        setRef("");
        setLabel("");
        setTried(false);
        void qc.invalidateQueries({ queryKey: contentKeys.all });
      })
      .catch((err: unknown) => toast.error("اضافه نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };
  return (
    <Card>
      <CardHeader
        icon={<Plus className="size-5" />}
        title="افزودن منبع"
        subtitle="کانال عمومی تلگرام (از صفحه‌ی عمومی‌اش، بی حساب تلگرام) یا خوراک RSS یک سایت خبری. هر ۵ دقیقه خوانده میشود."
      />
      <form onSubmit={submit} className="flex flex-col gap-4 p-4 sm:p-5" noValidate>
        <Segmented<SourceKind>
          value={kind}
          onChange={(v) => {
            setKind(v);
            setTried(false);
          }}
          label="نوع منبع"
          options={[
            { value: "telegram", label: "کانال تلگرام" },
            { value: "rss", label: "RSS" },
          ]}
        />
        <div className="grid gap-4 md:grid-cols-2">
          <Field
            label={kind === "telegram" ? "نام یا لینک کانال" : "نشانی RSS"}
            htmlFor="src-ref"
            error={tried ? problem : null}
            hint={kind === "telegram" ? "فقط کانال عمومی که پیش‌نمایش وبش باز است (t.me/s/نام)." : "فقط https."}
          >
            <TextInput
              id="src-ref"
              latin
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder={PLACEHOLDER[kind]}
              invalid={tried && Boolean(problem)}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="نام نمایشی (اختیاری)" htmlFor="src-label" hint="در صف بازبینی کنار هر خبر میآید.">
            <TextInput id="src-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} />
          </Field>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" icon={<Plus className="size-4" />} loading={busy} disabled={full}>
            افزودن
          </Button>
          {full ? <span className="text-sm text-warning">به سقف ۵۰ منبع رسیده‌اید؛ یکی را حذف کنید.</span> : null}
        </div>
        <div className="flex gap-2.5 rounded-2xl bg-surface-2 p-3.5 text-sm leading-7 text-fg-muted">
          <Camera className="mt-1 size-4 shrink-0" />
          <span>
            اینستاگرام بعداً اضافه میشود: خواندن پیج دیگران یک حساب جدای اینستاگرام لازم دارد. تا آن موقع، بیشتر پیج‌های خبری
            کانال تلگرام هم دارند.
          </span>
        </div>
      </form>
    </Card>
  );
}

function SourceRow({ s, index }: { s: ContentSource; index: number }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState<"toggle" | "fetch" | "remove" | null>(null);
  const [confirm, setConfirm] = useState(false);
  const health = sourceHealth(s);
  const refresh = () => void qc.invalidateQueries({ queryKey: contentKeys.all });
  const run = (what: "toggle" | "fetch" | "remove", p: Promise<unknown>, done: string) => {
    setBusy(what);
    p.then(() => {
      toast.success(done);
      refresh();
    })
      .catch((err: unknown) => toast.error("انجام نشد", errorMessage(err)))
      .finally(() => {
        setBusy(null);
        setConfirm(false);
      });
  };
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.04 * Math.min(index, 8) }}
      className="rounded-2xl border border-line bg-surface-2/60 p-3.5"
    >
      <div className="flex items-start gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">
          {s.kind === "telegram" ? <Send className="size-5" /> : <Rss className="size-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[0.95rem] font-bold text-fg">{s.label}</span>
            <Badge tone="neutral">{KIND_LABEL[s.kind]}</Badge>
            <Badge tone={health.tone} dot>
              {health.label}
            </Badge>
          </div>
          <a
            href={s.link}
            target="_blank"
            rel="noreferrer noopener"
            dir="ltr"
            className="mt-0.5 inline-flex max-w-full items-center gap-1 truncate text-[0.82rem] text-brand hover:underline"
          >
            <ExternalLink className="size-3.5 shrink-0" />
            <span className="truncate">{s.kind === "telegram" ? `t.me/${s.ref}` : s.ref}</span>
          </a>
          <p className="mt-1 text-[0.82rem] leading-6 text-fg-muted">
            {s.last_ok_at ? `آخرین خواندن موفق ${formatRelative(s.last_ok_at)}` : "هنوز خوانده نشده"}
            {s.last_ok_at ? ` · ${toFaDigits(s.last_count)} پست در صفحه` : ""}
            {s.pending > 0 ? (
              <>
                {" · "}
                <Link to="/review" className="text-brand hover:underline">
                  {toFaDigits(s.pending)} مورد در صف بازبینی
                </Link>
              </>
            ) : null}
          </p>
          {s.last_error_fa ? (
            <p role="status" className="mt-1.5 rounded-xl bg-danger-soft px-3 py-2 text-[0.82rem] leading-6 text-fg">
              {s.last_error_fa}
              {s.failures > 1 ? ` (${toFaDigits(s.failures)} بار پشت سر هم)` : ""}
              {s.next_fetch_at ? ` تلاش بعدی ${formatRelative(s.next_fetch_at)}.` : ""}
            </p>
          ) : null}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Toggle
          id={`src-on-${String(s.id)}`}
          checked={s.enabled}
          disabled={busy !== null}
          onCheckedChange={(v) => run("toggle", updateSource(s.id, { enabled: v }), v ? "روشن شد" : "خاموش شد؛ دیگر خوانده نمیشود")}
          label={s.enabled ? "روشن" : "خاموش"}
        />
        <Button
          variant="secondary"
          size="sm"
          icon={<RefreshCw className="size-4" />}
          loading={busy === "fetch"}
          disabled={!s.enabled || busy !== null}
          onClick={() => run("fetch", fetchSourceNow(s.id), "در دور بعد (حداکثر یک دقیقه) خوانده میشود")}
        >
          الان بخوان
        </Button>
        <Button
          variant="ghost"
          size="sm"
          icon={<Trash className="size-4" />}
          disabled={busy !== null}
          onClick={() => setConfirm(true)}
        >
          حذف
        </Button>
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        tone="danger"
        icon={<Trash className="size-5" />}
        title={`حذف «${s.label}»؟`}
        description="این منبع دیگر خوانده نمیشود و خبرهایی از آن که هنوز بازبینی نشده‌اند از صف برداشته میشوند. خبرهای تأییدشده یا رفته میمانند. پستی از کانال پاک نمیشود."
        confirmLabel="حذف منبع"
        busy={busy === "remove"}
        onConfirm={() => run("remove", removeSource(s.id), "منبع حذف شد")}
      />
    </motion.li>
  );
}

export default function SourcesPage() {
  const sources = useSources();
  const data = sources.data;
  return (
    <>
      <PageHeader
        title="منبع‌های محتوا"
        subtitle="جاهایی که خبر و سرگرمی پست‌ها از آن‌ها خوانده میشود. هر چیزی که میآید اول به صف بازبینی میرود و فقط با تأیید شما به کانال میرسد."
      />
      <div className="flex flex-col gap-4 sm:gap-5">
        <AddSource full={(data?.sources.length ?? 0) >= (data?.max_sources ?? 50)} />
        <Card>
          <CardHeader
            icon={<Rss className="size-5" />}
            title="منبع‌ها"
            subtitle={
              data
                ? `${toFaDigits(data.sources.length)} منبع؛ هر ${toFaDigits(data.fetch_every_min)} دقیقه خوانده میشوند. پس از خطا، فاصله‌ی تلاش‌ها بیشتر میشود.`
                : undefined
            }
            action={
              <Link to="/review" className="inline-flex items-center gap-1.5 text-sm font-semibold text-brand hover:underline">
                <Inbox className="size-4" />
                صف بازبینی
              </Link>
            }
          />
          <div className="p-4 sm:p-5">
            {sources.isPending ? (
              <SkeletonLines lines={5} />
            ) : sources.isError ? (
              <LoadError error={sources.error} compact onRetry={() => void sources.refetch()} notYet={NOT_YET} />
            ) : data && data.sources.length === 0 ? (
              <EmptyState
                icon={<Rss className="size-7" />}
                title="هنوز منبعی نیست"
                description="یک کانال عمومی تلگرام یا خوراک RSS اضافه کنید."
              />
            ) : (
              <ul className="grid gap-2.5 xl:grid-cols-2">
                {data?.sources.map((s, i) => (
                  <SourceRow key={s.id} s={s} index={i} />
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
