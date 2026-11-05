// Posting through the second Telegram account with Premium (decision D38), on the Content page: who
// posts new posts, the owner's custom-emoji pack, which emoji of the next post become animated, the
// account's admin rights in each channel (warnings for a missing post/edit right or a present delete
// right), and the recent new posts that went with the bot instead, with the reason in Persian.

import { useQueryClient } from "@tanstack/react-query";
import { Bot, CircleAlert, CircleCheck, CircleX, History, RefreshCw, Save, ShieldCheck, Smile, Sparkles } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import { formatJalali, formatRelative, toFaDigits } from "../lib/format";
import {
  checkOperator,
  effectivePoster,
  ownPackText,
  packErrorText,
  parsePackLink,
  POSTER_LABEL,
  putOperator,
  readyText,
  rightsStatus,
  warningText,
  type OperatorChannel,
  type OperatorView,
  type PosterName,
} from "../lib/operatorPoster";
import { ROLE_LABEL, type PostPreview, type PostRole } from "../lib/posts";
import { postsKeys, useOperator } from "../lib/postsQueries";
import { toast } from "../lib/toast";
import { useNow } from "../lib/useNow";
import { Button } from "./ui/Button";
import { Badge, Card, CardHeader } from "./ui/Card";
import { LoadError, SkeletonLines } from "./ui/Feedback";
import { Field, Segmented, TextInput } from "./ui/Form";

const NOT_YET = {
  title: "پست با حساب دوم هنوز روی پنل فعال نشده",
  description: "با به‌روزرسانی بعدی پنل، اینجا میتوانید پست‌ها را با ایموجی متحرک از حساب دوم بفرستید. کاری از شما لازم نیست.",
};

function Notice({ tone, children }: { tone: "warning" | "info" | "danger"; children: ReactNode }) {
  const style = { danger: "bg-danger-soft text-danger", warning: "bg-warning-soft text-warning", info: "bg-info-soft text-info" }[tone];
  return (
    <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className={cn("flex gap-2.5 rounded-2xl p-3 text-sm leading-7", style)}>
      <CircleAlert className="mt-1 size-4 shrink-0" />
      <div className="min-w-0 flex-1 text-fg">{children}</div>
    </motion.div>
  );
}

function Section({ title, icon, children, action }: { title: string; icon: ReactNode; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 rounded-2xl border border-line bg-surface-2/60 p-4">
      <div className="flex items-center gap-2">
        <span className="text-brand">{icon}</span>
        <span className="min-w-0 flex-1 text-sm font-bold text-fg">{title}</span>
        {action}
      </div>
      {children}
    </div>
  );
}

const ROLES: PostRole[] = ["test", "main"];

// --- who posts -------------------------------------------------------------------------------------------------

function PosterChoice({ v, busy, onSave }: { v: OperatorView; busy: boolean; onSave: (p: PosterName) => void }) {
  const roles = ROLES.filter((r) => v.channels[r]);
  return (
    <Section title="فرستنده‌ی پست‌های تازه" icon={<Bot className="size-4" />}>
      <Segmented<PosterName>
        value={v.poster}
        onChange={(p) => {
          if (!busy && p !== v.poster) onSave(p);
        }}
        options={(["bot", "operator"] as const).map((p) => ({ value: p, label: POSTER_LABEL[p] }))}
        label="فرستنده‌ی پست‌ها"
        className="w-full sm:w-auto"
      />
      <p className="text-[0.82rem] leading-6 text-fg-muted">
        {v.poster === "operator"
          ? "پست‌های تازه از حساب دوم با ایموجی متحرک میروند. هر جا حساب دوم آماده نباشد، همان پست با ربات میرود و دلیلش پایین همین کارت میآید. هر پست را همان کسی ویرایش میکند که فرستاده است."
          : "پست‌ها با ربات و ایموجی ساده میروند. حساب دوم فقط وقتی پست میگذارد که اینجا «حساب دوم» را انتخاب کنید."}
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={v.account.state === "connected" ? "success" : "warning"} dot>
          {v.account.state === "connected" ? `حساب دوم وصل است${v.account.first_name ? ` (${v.account.first_name})` : ""}` : "حساب دوم وصل نیست"}
        </Badge>
        {v.account.kill_switch ? (
          <Badge tone="danger" dot>
            کلید توقف روشن
          </Badge>
        ) : null}
        {roles.map((r) => (
          <Badge key={r} tone={effectivePoster(v, r) === "operator" ? "brand" : "neutral"}>
            {ROLE_LABEL[r]}: {effectivePoster(v, r) === "operator" ? "حساب دوم" : "ربات"}
          </Badge>
        ))}
      </div>
      {v.poster === "operator" ? (
        <AnimatePresence>
          {roles.map((r) => {
            const why = readyText(v.channels[r]?.ready ?? null);
            return why ? (
              <Notice key={r} tone="warning">
                <b>{ROLE_LABEL[r]}:</b> {why}
              </Notice>
            ) : null;
          })}
        </AnimatePresence>
      ) : null}
    </Section>
  );
}

// --- the emoji pack ------------------------------------------------------------------------------------------------

function PackForm({ v, busy, onSave }: { v: OperatorView; busy: boolean; onSave: (link: string) => Promise<unknown> }) {
  const [draft, setDraft] = useState(v.pack_link ?? "");
  const [submitted, setSubmitted] = useState(false);
  const parsed = parsePackLink(draft);
  const error = submitted && !parsed.ok ? parsed.error : null;
  const changed = parsed.ok ? parsed.shortName !== v.emoji_pack : draft.trim() !== (v.pack_link ?? "");
  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!parsed.ok || busy || !changed) return;
    void onSave(draft.trim()).then(
      () => setSubmitted(false),
      () => undefined,
    );
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-2">
      <Field
        label="لینک بسته‌ی ایموجی"
        htmlFor="op-pack"
        error={error}
        hint={
          <>
            در تلگرام روی یک ایموجی متحرک از بسته‌ی دلخواه بزنید، بسته را باز کنید و «Copy Link» را بزنید؛ لینکی مثل{" "}
            <span dir="ltr" className="inline-block">
              https://t.me/addemoji/…
            </span>
            . خالی بگذارید تا بسته‌ی اختصاصی کانال به‌کار رود.
          </>
        }
      >
        <TextInput
          id="op-pack"
          latin
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          invalid={Boolean(error)}
          placeholder="https://t.me/addemoji/…"
          autoComplete="off"
          spellCheck={false}
        />
      </Field>
      {changed ? (
        <Button type="submit" size="sm" loading={busy} icon={<Save className="size-4" />} className="self-start">
          {parsed.ok && parsed.shortName === null ? "برداشتن بسته" : "ذخیره‌ی لینک"}
        </Button>
      ) : null}
    </form>
  );
}

function OwnPackLine({ v, busy, onUse }: { v: OperatorView; busy: boolean; onUse: (link: string) => void }) {
  const own = v.own_pack;
  if (!own) return null;
  const { tone, text } = ownPackText(own);
  const inUse = own.name !== null && v.emoji_pack?.toLowerCase() === own.name.toLowerCase();
  return (
    <div className="flex flex-col gap-1.5 rounded-xl bg-surface p-3 ring-1 ring-line">
      <div className="flex flex-wrap items-center gap-1.5 text-[0.82rem]">
        <Sparkles className="size-4 text-brand" aria-hidden />
        <b>بسته‌ی اختصاصی کانال</b>
        <Badge tone={tone}>{inUse ? "در حال استفاده" : own.state === "ready" ? "آماده" : "در انتظار"}</Badge>
      </div>
      <p className="text-[0.82rem] leading-6 text-fg-muted">
        ایموجی‌های متحرک اینترنت و اپراتورها، با عنوان کانال. {text}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {own.link ? (
          <a href={own.link} target="_blank" rel="noopener noreferrer" dir="ltr" className="text-[0.8rem] font-bold text-brand hover:underline">
            {own.link}
          </a>
        ) : null}
        {own.link && own.state === "ready" && !inUse ? (
          <Button size="sm" variant="secondary" loading={busy} onClick={() => onUse(own.link ?? "")}>
            استفاده از این بسته
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function PackSummary({ v, now }: { v: OperatorView; now: Date }) {
  const pack = v.pack;
  if (!v.emoji_pack) return <p className="text-[0.82rem] leading-6 text-fg-muted">هنوز بسته‌ای انتخاب نشده؛ همه‌ی ایموجی‌ها ساده میمانند.</p>;
  if (!pack) return <p className="text-[0.82rem] leading-6 text-fg-muted">بسته هنوز از تلگرام خوانده نشده؛ «بررسی حالا» را بزنید یا تا بررسی خودکار بعدی صبر کنید.</p>;
  const problem = packErrorText(pack.error, pack.telegram_error);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5 text-[0.82rem] text-fg-muted">
        <Badge tone={pack.count > 0 ? "success" : "warning"}>{pack.title || pack.short_name}</Badge>
        <span>{toFaDigits(pack.count)} ایموجی</span>
        {pack.fetched_at ? <span title={formatJalali(pack.fetched_at)}>— خوانده‌شده {formatRelative(pack.fetched_at, now)}</span> : null}
      </div>
      {problem ? <Notice tone="warning">{problem}</Notice> : null}
      {pack.emoji.length > 0 ? (
        <div className="flex max-h-28 flex-wrap gap-1 overflow-y-auto" aria-label="ایموجی‌های بسته">
          {pack.emoji.map((e) => (
            <span key={e.id} className="grid size-8 place-items-center rounded-lg bg-surface text-lg ring-1 ring-line" title={e.emoji}>
              {e.emoji}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function NextPostEmoji({ preview, v }: { preview: PostPreview | undefined; v: OperatorView }) {
  const list = preview?.emoji;
  if (!list) return null;
  if (list.length === 0) return <p className="text-[0.82rem] leading-6 text-fg-muted">پست بعدی ایموجی ندارد.</p>;
  const animated = list.filter((e) => e.animated).length;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[0.82rem] leading-6 text-fg-muted">
        {v.poster === "operator"
          ? `در پست بعدی ${toFaDigits(animated)} ایموجی از ${toFaDigits(list.length)} متحرک میشود (پیش‌نمایش بالا ایموجی‌ها را ساده نشان میدهد).`
          : "اگر فرستنده «حساب دوم» باشد، این ایموجی‌های پست بعدی متحرک میشوند:"}
      </p>
      <ul className="flex flex-wrap gap-1.5">
        {list.map((e) => (
          <li
            key={e.emoji}
            className={cn(
              "inline-flex items-center gap-1 rounded-xl px-2 py-1 text-[0.78rem] font-semibold ring-1",
              e.animated ? "bg-brand-soft text-brand ring-brand/30" : "bg-surface text-fg-muted ring-line",
            )}
          >
            <span className="text-base">{e.emoji}</span>
            {e.animated ? (
              <>
                <Sparkles className="size-3.5" aria-hidden />
                متحرک
              </>
            ) : (
              "ساده"
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- the account's rights -----------------------------------------------------------------------------------------

function Right({ label, ok, good }: { label: string; ok: boolean | null; good: boolean }) {
  const fine = ok === null ? null : ok === good;
  return (
    <span className={cn("inline-flex items-center gap-1 text-[0.8rem]", fine === null ? "text-fg-faint" : fine ? "text-success" : "text-danger")}>
      {fine === null ? <CircleAlert className="size-3.5" /> : ok ? <CircleCheck className="size-3.5" /> : <CircleX className="size-3.5" />}
      {label}
    </span>
  );
}

function ChannelRightsRow({ role, ch }: { role: PostRole; ch: OperatorChannel }) {
  const status = rightsStatus(ch.rights);
  const r = ch.rights;
  const admin = r?.status === "admin" || r?.status === "creator";
  return (
    <li className="flex flex-col gap-2 rounded-xl bg-surface p-3 ring-1 ring-line">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-bold text-fg">{ROLE_LABEL[role]}</span>
        <span dir="ltr" className="text-xs text-fg-muted">
          {ch.username}
        </span>
        <Badge tone={status.tone} className="ms-auto">
          {status.label}
        </Badge>
      </div>
      {admin ? (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <Right label="ارسال پست" ok={r.can_post} good />
          <Right label="ویرایش پست" ok={r.can_edit} good />
          <Right label={r.can_delete ? "حذف پیام (باید خاموش باشد)" : "حذف پیام: ندارد"} ok={r.can_delete} good={false} />
        </div>
      ) : null}
      {ch.warnings.map((w) => (
        <Notice key={w} tone={w === "delete_right" ? "danger" : "warning"}>
          {warningText(w)}
        </Notice>
      ))}
    </li>
  );
}

// --- the card ----------------------------------------------------------------------------------------------------------

function Body({ v, preview }: { v: OperatorView; preview: PostPreview | undefined }) {
  const qc = useQueryClient();
  const now = useNow(60_000);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const roles = ROLES.filter((r) => v.channels[r]);

  const apply = (out: OperatorView) => {
    qc.setQueryData(postsKeys.operator, out);
    void qc.invalidateQueries({ queryKey: postsKeys.preview });
    void qc.invalidateQueries({ queryKey: ["events"] });
  };
  const save = (input: Parameters<typeof putOperator>[0], done: string) => {
    setBusy(true);
    return putOperator(input)
      .then((out) => {
        apply(out);
        toast.success(done);
        return out;
      })
      .catch((err: unknown) => {
        toast.error("ذخیره نشد", errorMessage(err));
        throw err;
      })
      .finally(() => setBusy(false));
  };
  const check = () => {
    setChecking(true);
    checkOperator()
      .then((out) => {
        apply(out);
        toast.success("بسته و دسترسی‌ها از تلگرام خوانده شد");
      })
      .catch((err: unknown) => {
        void qc.invalidateQueries({ queryKey: postsKeys.operator });
        toast.error("بررسی انجام نشد", errorMessage(err));
      })
      .finally(() => setChecking(false));
  };

  return (
    <div className="flex flex-col gap-4 p-4 sm:p-5">
      <PosterChoice
        v={v}
        busy={busy}
        onSave={(p) => void save({ poster: p }, p === "operator" ? "از این پس پست‌های تازه با حساب دوم میروند" : "از این پس پست‌های تازه با ربات میروند").catch(() => undefined)}
      />
      <Section
        title="ایموجی‌های متحرک"
        icon={<Smile className="size-4" />}
        action={
          <Button variant="secondary" size="sm" loading={checking} icon={<RefreshCw className="size-4" />} onClick={check}>
            بررسی حالا
          </Button>
        }
      >
        <OwnPackLine v={v} busy={busy} onUse={(link) => void save({ pack_link: link }, "بسته‌ی اختصاصی کانال انتخاب شد").catch(() => undefined)} />
        <PackForm key={v.pack_link ?? ""} v={v} busy={busy} onSave={(link) => save({ pack_link: link }, link ? "لینک بسته ذخیره شد؛ بسته از تلگرام خوانده میشود" : "بسته برداشته شد")} />
        <PackSummary v={v} now={now} />
        <NextPostEmoji preview={preview} v={v} />
      </Section>
      <Section title="دسترسی حساب دوم در کانال‌ها" icon={<ShieldCheck className="size-4" />}>
        {roles.length === 0 ? (
          <p className="text-[0.82rem] leading-6 text-fg-muted">هنوز کانالی ثبت نشده (صفحه‌ی تلگرام).</p>
        ) : (
          <>
            <p className="text-[0.82rem] leading-6 text-fg-muted">
              حساب دوم باید در هر کانال ادمین باشد، با «ارسال پست» و «ویرایش پست» و <b>بدون</b> «حذف پیام».
              {v.rights_checked_at ? ` آخرین بررسی: ${formatRelative(v.rights_checked_at, now)}.` : " هنوز بررسی نشده؛ «بررسی حالا» را بزنید."}
            </p>
            <ul className="flex flex-col gap-2">
              {roles.map((r) => {
                const ch = v.channels[r];
                return ch ? <ChannelRightsRow key={r} role={r} ch={ch} /> : null;
              })}
            </ul>
          </>
        )}
      </Section>
      {v.fallbacks.length > 0 ? (
        <Section title="پست‌هایی که به جای حساب دوم با ربات رفتند" icon={<History className="size-4" />}>
          <ul className="flex flex-col gap-1.5">
            {v.fallbacks.map((f, i) => (
              <li key={`${f.at ?? ""}-${String(i)}`} className="flex flex-col gap-0.5 rounded-xl bg-surface p-2.5 ring-1 ring-line sm:flex-row sm:items-start sm:gap-3">
                <span className="shrink-0 text-xs text-fg-faint" title={f.at ? formatJalali(f.at) : undefined}>
                  {f.at ? formatRelative(f.at, now) : "—"}
                  {f.role ? ` — ${ROLE_LABEL[f.role]}` : ""}
                </span>
                <span className="text-[0.84rem] leading-6 text-fg">{f.text_fa}</span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

export function OperatorPosterCard({ preview }: { preview: PostPreview | undefined }) {
  const q = useOperator();
  return (
    <Card>
      <CardHeader
        icon={<Sparkles className="size-5" />}
        title="پست با حساب دوم (Premium)"
        subtitle="ایموجی‌های متحرک فقط از حساب دوم ممکن است؛ ربات پشتیبان میماند. هیچ پستی حذف نمیشود، فقط ویرایش."
        action={
          q.data ? (
            <span className="hidden sm:block">
              <Badge tone={q.data.poster === "operator" ? "brand" : "neutral"} dot>
                {POSTER_LABEL[q.data.poster]}
              </Badge>
            </span>
          ) : null
        }
      />
      {q.isPending ? (
        <div className="p-5">
          <SkeletonLines lines={5} />
        </div>
      ) : q.isError ? (
        <LoadError error={q.error} compact onRetry={() => void q.refetch()} notYet={NOT_YET} />
      ) : (
        <Body v={q.data} preview={preview} />
      )}
    </Card>
  );
}
