import { useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  CircleCheck,
  CircleX,
  ExternalLink,
  Eye,
  ImageOff,
  Inbox,
  Layers,
  Play,
  Rss,
  Save,
  ShieldCheck,
  Undo2,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";

import { QcSupervisorCard } from "../components/QcSupervisorCard";
import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { EmptyState, LoadError, Skeleton } from "../components/ui/Feedback";
import { Field, Segmented, TextInput } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import {
  CATEGORY_LABEL,
  captionErrors,
  captionLength,
  durationText,
  editCandidate,
  KIND_LABEL,
  MEDIA_STATE_TEXT,
  mediaUrl,
  FLAG_LABEL,
  qcVerdict,
  setCandidateStatus,
  sizeText,
  SORT_LABEL,
  STATUS_INFO,
  viewsText,
  type Candidate,
  type CandidateStatus,
  type QueueSort,
} from "../lib/contentEngine";
import { contentKeys, useQueue } from "../lib/contentEngineQueries";
import { formatRelative, toFaDigits } from "../lib/format";
import { postsKeys } from "../lib/postsQueries";
import { toast } from "../lib/toast";

const NOT_YET = {
  title: "صف بازبینی هنوز روی پنل فعال نشده",
  description: "موتور محتوا با به‌روزرسانی بعدی پنل فعال میشود. کاری از شما لازم نیست.",
};

const STATUSES: CandidateStatus[] = ["pending", "approved", "rejected", "used", "expired"];

function Note({ tone, children }: { tone: "warning" | "info"; children: ReactNode }) {
  return (
    <div className={cn("rounded-2xl p-3.5 text-sm leading-7 text-fg", tone === "warning" ? "bg-warning-soft" : "bg-info-soft")}>
      {children}
    </div>
  );
}

function Preview({ c }: { c: Candidate }) {
  const m = c.media;
  if (!m) {
    return (
      <div className="grid aspect-video place-items-center rounded-2xl bg-surface-3 text-sm text-fg-faint">
        <span className="flex items-center gap-1.5">
          <ImageOff className="size-4" />
          بی عکس و ویدیو؛ پست متنی میرود
        </span>
      </div>
    );
  }
  const duration = durationText(m.duration);
  return (
    <div className="relative overflow-hidden rounded-2xl bg-surface-3">
      {m.preview_id !== null ? (
        <img
          src={mediaUrl(m.preview_id)}
          alt={c.title || "پیش‌نمایش"}
          loading="lazy"
          className="aspect-video w-full object-contain"
        />
      ) : (
        <div className="grid aspect-video place-items-center text-sm text-fg-faint">
          <span className="flex items-center gap-1.5">
            <ImageOff className="size-4" />
            {m.state === "failed" ? "رسانه دانلود نشد" : "پیش‌نمایش در حال دانلود"}
          </span>
        </div>
      )}
      {m.kind === "video" ? (
        <span className="absolute start-2 top-2 inline-flex items-center gap-1 rounded-full bg-[rgb(0_0_0/0.6)] px-2 py-1 text-xs font-semibold text-white">
          <Play className="size-3.5" />
          ویدیو{duration ? ` ${duration}` : ""}
        </span>
      ) : null}
      <div className="absolute end-2 bottom-2 flex flex-wrap gap-1">
        {m.state !== "ready" ? (
          <Badge tone={m.state === "failed" ? "danger" : "info"}>{MEDIA_STATE_TEXT[m.state]}</Badge>
        ) : null}
        {m.width && m.height ? (
          <Badge tone={Math.min(m.width, m.height) < 320 ? "warning" : "neutral"}>
            {toFaDigits(`${String(m.width)}×${String(m.height)}`)}
          </Badge>
        ) : null}
        {m.size ? <Badge tone="neutral">{sizeText(m.size)}</Badge> : null}
      </div>
    </div>
  );
}

/** The supervisor's review of one candidate (D52): its decision, Claude's reasons and every check. */
function QcBox({ c }: { c: Candidate }) {
  const q = c.qc;
  const verdict = qcVerdict(c);
  if (!q || !verdict) return null;
  const backend = q.backend === "bridge" ? "اشتراک Claude" : q.backend === "api" ? "کلید API" : "قاعده‌ی قطعی";
  return (
    <div className="flex flex-col gap-1.5 rounded-2xl bg-surface-2 p-3 text-[0.85rem] leading-7">
      <div className="flex flex-wrap items-center gap-1.5">
        <ShieldCheck className="size-4 text-fg-muted" />
        <Badge tone={verdict.tone}>{verdict.label}</Badge>
        {q.category ? <Badge tone="neutral">{CATEGORY_LABEL[q.category] ?? q.category}</Badge> : null}
        <span className="text-[0.78rem] text-fg-faint">
          {backend}
          {q.at ? ` · ${formatRelative(q.at)}` : ""}
          {c.decided_by === "owner" ? " · تصمیم آخر با شما" : ""}
        </span>
      </div>
      {q.reasons.length > 0 ? (
        <ul className="list-inside list-disc text-fg">
          {q.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
      {q.flags.length > 0 ? (
        <p className="text-danger">پرچم‌ها: {q.flags.map((f) => FLAG_LABEL[f] ?? f).join("، ")}</p>
      ) : null}
      {q.checks_fa.length > 0 ? <p className="text-warning">بررسی کد: {q.checks_fa.join(" ")}</p> : null}
      {q.error_fa && q.decision === null ? <p className="text-fg-muted">{q.error_fa}</p> : null}
    </div>
  );
}

function CandidateCard({ c, budget, index }: { c: Candidate; budget: number | null; index: number }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState(c.title);
  const [body, setBody] = useState(c.body);
  const [busy, setBusy] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);
  const editable = c.status === "pending" || c.status === "approved";
  const dirty = title !== c.title || body !== c.body;
  const errors = captionErrors(title, body);
  const valid = !errors.title && !errors.body;
  const length = captionLength(title, body);
  const over = budget !== null && c.media?.state === "ready" && length > budget;
  const views = viewsText(c.views);
  const info = STATUS_INFO[c.status];
  const done = (text: string) => {
    toast.success(text);
    void qc.invalidateQueries({ queryKey: contentKeys.all });
    void qc.invalidateQueries({ queryKey: postsKeys.all });
  };
  const act = (what: string, p: Promise<unknown>, text: string) => {
    setBusy(what);
    p.then(() => done(text))
      .catch((err: unknown) => toast.error("انجام نشد", errorMessage(err)))
      .finally(() => setBusy(null));
  };
  const caption = { title: title.trim(), body: body.trim() };
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.04 * Math.min(index, 8), duration: 0.35 }}
      className="flex flex-col gap-3 rounded-3xl border border-line bg-surface p-3.5 shadow-card sm:p-4"
    >
      <Preview c={c} />
      <div className="flex flex-wrap items-center gap-1.5 text-[0.82rem] text-fg-muted">
        <Badge tone={info.tone}>{info.label}</Badge>
        <Badge tone="neutral">
          {c.source.kind === "rss" ? <Rss className="size-3" /> : null}
          {c.source.label}
        </Badge>
        <span>{formatRelative(c.published_at ?? c.created_at)}</span>
        {views ? (
          <span className="inline-flex items-center gap-1">
            <Eye className="size-3.5" />
            {views}
          </span>
        ) : null}
        {c.url ? (
          <a
            href={c.url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-brand hover:underline"
          >
            <ExternalLink className="size-3.5" />
            {c.source.kind === "telegram" ? "پست اصلی" : "خبر اصلی"}
          </a>
        ) : null}
      </div>
      {c.also_in.length > 0 ? (
        <p className="flex items-start gap-1.5 text-[0.82rem] leading-6 text-fg-muted">
          <Layers className="mt-1 size-3.5 shrink-0" />
          همین خبر در {c.also_in.join("، ")} هم آمد.
        </p>
      ) : null}
      <QcBox c={c} />
      {c.media?.error_fa ? <Note tone="warning">{c.media.error_fa} اگر تأیید کنید، پست بی رسانه میرود.</Note> : null}
      <Field label="عنوان (پررنگ، با یک ایموجی)" htmlFor={`cand-title-${String(c.id)}`} error={errors.title ?? null}>
        <TextInput
          id={`cand-title-${String(c.id)}`}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          disabled={!editable}
          invalid={Boolean(errors.title)}
          maxLength={140}
        />
      </Field>
      <Field
        label="متن"
        htmlFor={`cand-body-${String(c.id)}`}
        error={errors.body ?? null}
        hint={
          budget !== null && c.media
            ? `${toFaDigits(length)} از حدود ${toFaDigits(budget)} نویسه‌ای که کپشن با لینک‌های فعلی جا دارد${over ? "؛ بیشترش هنگام پست کوتاه میشود، لینک‌ها هرگز" : ""}.`
            : `${toFaDigits(length)} نویسه`
        }
      >
        <textarea
          id={`cand-body-${String(c.id)}`}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          disabled={!editable}
          rows={5}
          className={cn(
            "w-full rounded-xl border bg-surface-2 p-3 text-[0.95rem] leading-7 text-fg outline-none focus:border-brand focus:bg-surface focus:ring-4 focus:ring-[var(--ph-ring)] disabled:opacity-70",
            errors.body ? "border-danger" : over ? "border-warning" : "border-line",
          )}
        />
      </Field>
      {c.source_text ? (
        <div>
          <button
            type="button"
            onClick={() => setShowSource((v) => !v)}
            className="inline-flex items-center gap-1 text-[0.82rem] font-semibold text-fg-muted hover:text-fg"
            aria-expanded={showSource}
          >
            <ChevronDown className={cn("size-4 transition-transform", showSource && "rotate-180")} />
            متن اصلی منبع (پاک‌شده از لینک و امضا)
          </button>
          <AnimatePresence initial={false}>
            {showSource ? (
              <motion.p
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                className="mt-1.5 overflow-hidden rounded-xl bg-surface-2 p-3 text-[0.85rem] leading-7 whitespace-pre-wrap text-fg-muted"
              >
                {c.source_text}
              </motion.p>
            ) : null}
          </AnimatePresence>
        </div>
      ) : null}
      {editable || c.status === "rejected" ? (
        <div className="mt-auto flex flex-wrap gap-1.5">
          {c.status === "pending" ? (
            <Button
              variant="success"
              size="sm"
              icon={<CircleCheck className="size-4" />}
              loading={busy === "approve"}
              disabled={!valid || busy !== null}
              onClick={() => act("approve", setCandidateStatus(c.id, "approved", caption), "تأیید شد؛ محتوای یکی از پست‌های بعدی میشود")}
            >
              تأیید
            </Button>
          ) : null}
          {editable && dirty ? (
            <Button
              variant="secondary"
              size="sm"
              icon={<Save className="size-4" />}
              loading={busy === "save"}
              disabled={!valid || busy !== null}
              onClick={() => act("save", editCandidate(c.id, caption), "ذخیره شد")}
            >
              ذخیره
            </Button>
          ) : null}
          {c.status === "approved" || c.status === "rejected" ? (
            <Button
              variant="ghost"
              size="sm"
              icon={<Undo2 className="size-4" />}
              loading={busy === "back"}
              disabled={busy !== null}
              onClick={() => act("back", setCandidateStatus(c.id, "pending"), "به صف برگشت")}
            >
              برگرداندن به صف
            </Button>
          ) : null}
          {c.status !== "rejected" ? (
            <Button
              variant="ghost"
              size="sm"
              icon={<CircleX className="size-4" />}
              loading={busy === "reject"}
              disabled={busy !== null}
              onClick={() => act("reject", setCandidateStatus(c.id, "rejected"), "رد شد")}
            >
              رد
            </Button>
          ) : null}
        </div>
      ) : null}
    </motion.li>
  );
}

export default function ReviewPage() {
  const [status, setStatus] = useState<CandidateStatus>("pending");
  const [sort, setSort] = useState<QueueSort>("new");
  const queue = useQueue(status, sort);
  const data = queue.data;
  const tabs = STATUSES.map((s) => ({
    value: s,
    label: `${STATUS_INFO[s].label}${data ? ` ${toFaDigits(data.counts[s])}` : ""}`,
  }));
  return (
    <>
      <PageHeader
        title="صف بازبینی"
        subtitle="خبرها و سرگرمی‌هایی که منبع‌ها آورده‌اند. ناظر خودکار (Claude و قاعده‌های قطعی) پیش از هر پست بهترین‌ها را بررسی میکند و کپشن کوتاه فارسی مینویسد. اینجا فقط برای دیدن و تغییر دستی است و لازم نیست. پیوند و امضای منبع پاک شده‌اند و لینک‌های پروکسی خودکار زیر کپشن میآیند."
      />
      <div className="flex flex-col gap-4 sm:gap-5">
        <QcSupervisorCard />
        <Card>
          <CardHeader
            icon={<Inbox className="size-5" />}
            title="بازبینی"
            subtitle={STATUS_INFO[status].hint}
            action={
              <Link to="/sources" className="inline-flex items-center gap-1.5 text-sm font-semibold text-brand hover:underline">
                <Rss className="size-4" />
                منبع‌ها
              </Link>
            }
          />
          <div className="flex flex-col gap-3 p-4 sm:p-5">
            <div className="-mx-1 overflow-x-auto px-1 pb-1">
              <Segmented<CandidateStatus> value={status} onChange={setStatus} options={tabs} label="وضعیت" className="min-w-max" />
            </div>
            {status === "pending" ? (
              <Segmented<QueueSort>
                value={sort}
                onChange={setSort}
                label="ترتیب"
                options={(["new", "views", "hot"] as const).map((s) => ({ value: s, label: SORT_LABEL[s] }))}
                className="self-start"
              />
            ) : null}
            {data && data.caption_budget !== null ? (
              <Note tone="info">
                پست عکس‌دار یا ویدیودار کپشن دارد و تلگرام کپشن را به ۱٬۰۲۴ نویسه محدود میکند. با لینک‌های فعلی، عنوان و متن روی هم
                حدود {toFaDigits(data.caption_budget)} نویسه جا دارند؛ بیشترش هنگام پست کوتاه میشود و لینک‌ها هرگز کوتاه نمیشوند.
              </Note>
            ) : null}
            {data && data.counts.approved === 0 && status === "pending" && data.items.length > 0 ? (
              <Note tone="info">
                الان موردی تأییدشده نیست؛ ناظر پیش از پست بعدی بهترین‌ها را بررسی میکند. اگر چیزی تأیید نشود، پست با بانک محتوا میرود.
              </Note>
            ) : null}
          </div>
        </Card>
        {queue.isPending ? (
          <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
            <Skeleton className="h-[34rem] rounded-3xl" />
            <Skeleton className="h-[34rem] rounded-3xl" />
          </div>
        ) : queue.isError ? (
          <Card>
            <LoadError error={queue.error} onRetry={() => void queue.refetch()} notYet={NOT_YET} />
          </Card>
        ) : data && data.items.length === 0 ? (
          <Card>
            <EmptyState
              icon={<Inbox className="size-7" />}
              title="اینجا خالی است"
              description={
                status === "pending"
                  ? "خبر تازه‌ای در صف نیست. منبع‌ها هر ۵ دقیقه خوانده میشوند؛ منبع تازه را در صفحه‌ی منبع‌ها اضافه کنید."
                  : "موردی با این وضعیت نیست."
              }
            />
          </Card>
        ) : (
          <ul className="grid items-start gap-4 md:grid-cols-2 2xl:grid-cols-3">
            {data?.items.map((c, i) => (
              <CandidateCard key={`${String(c.id)}-${c.title}-${c.body}`} c={c} budget={data.caption_budget} index={i} />
            ))}
          </ul>
        )}
        <p className="text-[0.82rem] leading-6 text-fg-faint">
          منبع‌ها: {Object.values(KIND_LABEL).join(" و ")}. خبرهای بیش از ۴۸ ساعت کهنه میشوند. هیچ پستی از کانال پاک نمیشود.
        </p>
      </div>
    </>
  );
}
