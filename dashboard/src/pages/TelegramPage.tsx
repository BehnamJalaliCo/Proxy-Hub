import { useQueryClient } from "@tanstack/react-query";
import {
  BellRing,
  Bot,
  CircleAlert,
  CircleCheck,
  CircleX,
  ExternalLink,
  Inbox,
  KeyRound,
  Link2,
  Megaphone,
  MoonStar,
  Plus,
  Save,
  Send,
  ShieldAlert,
  Smartphone,
  Trash2,
  Undo2,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState, type ReactNode, type SubmitEvent } from "react";

import { MtproxyAccountCard } from "../components/MtproxyAccountCard";
import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader, StatusDot } from "../components/ui/Card";
import { CopyButton, LoadError, Skeleton, SkeletonLines } from "../components/ui/Feedback";
import { Field, SecretInput, TextInput, TimeSelect } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage, isApiError } from "../lib/api";
import { cn } from "../lib/browser";
import * as api from "../lib/endpoints";
import { formatJalali, formatNumber, formatRelative, toFaDigits } from "../lib/format";
import { botErrorText, type Tone } from "../lib/labels";
import { keys, useTelegramSettings, useTelegramStatus } from "../lib/queries";
import {
  activeWindow,
  botLink,
  botTokenProblem,
  crossesMidnight,
  faTime,
  formatCountdown,
  MAX_POST_INTERVAL_MIN,
  MAX_WINDOWS,
  normalizeChannel,
  pairingLink,
  sameSchedule,
  secondsLeft,
  segments,
  startCommand,
  tehranMinutes,
  validateSchedule,
  windowLengthText,
} from "../lib/telegram";
import { toast } from "../lib/toast";
import { TONE_SOFT } from "../lib/tones";
import { useNow } from "../lib/useNow";
import type { ChannelRole, QuietWindow, TgChannel, TgPairing, TgSettings, TgStatus } from "../lib/types";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.05 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <motion.p
      role="alert"
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-fg"
    >
      <CircleAlert className="mt-1 size-4 shrink-0 text-danger" />
      <span>{children}</span>
    </motion.p>
  );
}

// --- progress ----------------------------------------------------------------------------------

function SetupSteps({ s }: { s: TgStatus }) {
  const steps = [
    // `latin`: a @name is shown left-to-right, so its "@" stays in front and it truncates at its end.
    { done: s.bot !== null, label: "ربات", hint: s.bot ? `@${s.bot.username}` : "توکن BotFather", latin: s.bot !== null },
    { done: s.owner_paired, label: "چت شما", hint: s.owner_paired ? "هشدارها به شما میرسد" : "با کد اتصال", latin: false },
    { done: s.channels.test !== null, label: "کانال آزمایشی", hint: s.channels.test?.username ?? "تعیین نشده", latin: s.channels.test !== null },
    { done: s.channels.main !== null, label: "کانال اصلی", hint: s.channels.main?.username ?? "تعیین نشده", latin: s.channels.main !== null },
  ];
  const done = steps.filter((x) => x.done).length;
  return (
    <Card className="p-4 sm:p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <span className="text-sm font-bold text-fg">راه‌اندازی تلگرام</span>
        <Badge tone={done === steps.length ? "success" : "brand"}>
          {toFaDigits(done)} از {toFaDigits(steps.length)} گام
        </Badge>
      </div>
      <ol className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {steps.map((x, i) => (
          <motion.li
            key={x.label}
            {...fadeUp(i)}
            className={cn("flex items-center gap-2.5 rounded-2xl border p-2.5", x.done ? "border-success/25 bg-success-soft" : "border-line bg-surface-2")}
          >
            <span className={cn("grid size-8 shrink-0 place-items-center rounded-xl text-sm font-bold", x.done ? "bg-success text-white" : "bg-surface-3 text-fg-muted")}>
              {x.done ? <CircleCheck className="size-4" /> : toFaDigits(i + 1)}
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-bold text-fg">{x.label}</span>
              <span dir={x.latin ? "ltr" : undefined} className={cn("block truncate text-xs text-fg-muted", x.latin && "text-right")}>
                {x.hint}
              </span>
            </span>
          </motion.li>
        ))}
      </ol>
    </Card>
  );
}

// --- bot ------------------------------------------------------------------------------------------

/** The token lives only in this form's state; it is sent once and always cleared. Never cached. */
function BotCard({ s }: { s: TgStatus }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [open, setOpen] = useState(s.bot === null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ field: boolean; text: string } | null>(null);

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const problem = botTokenProblem(token);
    if (problem) {
      setError({ field: true, text: problem });
      return;
    }
    setBusy(true);
    setError(null);
    api
      .putTelegramBot(token.trim())
      .then((status) => {
        qc.setQueryData(keys.telegram, status);
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("ربات ثبت شد", status.bot ? `‎@${status.bot.username}‎ با getMe وارسی شد.` : undefined);
        setOpen(false);
      })
      .catch((err: unknown) => {
        const code = isApiError(err) ? err.code : "";
        setError({ field: code === "bot_token_invalid", text: errorMessage(err) });
      })
      .finally(() => {
        // A token is never kept for another try, accepted or not.
        setToken("");
        setBusy(false);
      });
  };

  return (
    <Card className="lg:col-span-5">
      <CardHeader
        icon={<Bot className="size-5" />}
        title="ربات پست‌گذار"
        subtitle="ربات فقط ابزار پست گذاشتن و هشدار به شماست؛ کاربرها با آن کاری ندارند."
        action={s.bot ? <Badge tone={s.bot_error ? "danger" : "success"} dot>{s.bot_error ? "مشکل دارد" : "وصل"}</Badge> : <Badge tone="warning">تعیین نشده</Badge>}
      />
      <div className="flex flex-col gap-4 p-5">
        {s.bot ? (
          <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface-2 p-3">
            <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">
              <Bot className="size-5" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="ltr truncate text-right font-bold text-fg">@{s.bot.username}</div>
              <div className="text-xs text-fg-muted">
                شناسه‌ی ربات <span className="ltr inline-block">{s.bot.id}</span>
              </div>
            </div>
            {botLink(s.bot.username) ? (
              <a
                href={botLink(s.bot.username) ?? undefined}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-sm font-bold text-brand hover:bg-brand-soft"
              >
                باز کردن
                <ExternalLink className="size-4" />
              </a>
            ) : null}
          </div>
        ) : null}
        {s.bot_error ? <ErrorNote>{botErrorText(s.bot_error)}</ErrorNote> : null}

        {s.bot && !open ? (
          <Button variant="secondary" size="sm" className="self-start" icon={<KeyRound className="size-4" />} onClick={() => setOpen(true)}>
            عوض کردن توکن ربات
          </Button>
        ) : (
          <form onSubmit={submit} noValidate autoComplete="off" className="flex flex-col gap-3">
            {!s.bot ? (
              <ol className="flex list-inside list-[persian] flex-col gap-1 text-[0.84rem] leading-7 text-fg-muted marker:font-bold marker:text-brand">
                <li>
                  در تلگرام{" "}
                  <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer" className="font-bold text-brand hover:underline">
                    ‎@BotFather‎
                  </a>{" "}
                  را باز کنید و <code className="ltr inline-block rounded bg-surface-3 px-1">/newbot</code> را بفرستید.
                </li>
                <li>نام و نام کاربری ربات را بدهید؛ BotFather یک توکن میدهد.</li>
                <li>توکن را اینجا بچسبانید. پنل آن را با getMe میسنجد و در گاوصندوق نگه میدارد.</li>
              </ol>
            ) : null}
            <Field
              label="توکن ربات"
              htmlFor="tg-token"
              hint="پس از ذخیره، توکن هرگز دوباره نشان داده نمیشود."
              error={error?.field ? error.text : null}
            >
              <SecretInput
                id="tg-token"
                name="telegram-bot-token"
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
                allowReveal={false}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                invalid={Boolean(error?.field)}
                placeholder="••••••••••:•••••••••••••••••"
              />
            </Field>
            <AnimatePresence>{error && !error.field ? <ErrorNote>{error.text}</ErrorNote> : null}</AnimatePresence>
            <div className="flex flex-col-reverse gap-2 sm:flex-row">
              {s.bot ? (
                <Button
                  variant="secondary"
                  onClick={() => {
                    setOpen(false);
                    setToken("");
                    setError(null);
                  }}
                  disabled={busy}
                >
                  انصراف
                </Button>
              ) : null}
              <Button type="submit" className="sm:flex-1" loading={busy} icon={<KeyRound className="size-5" />}>
                وارسی و ذخیره
              </Button>
            </div>
          </form>
        )}
      </div>
    </Card>
  );
}

// --- owner pairing ----------------------------------------------------------------------------------

function PairingCard({ s, created, onCreated }: { s: TgStatus; created: TgPairing | null; onCreated: (p: TgPairing) => void }) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const pairing = s.pairing ?? created;
  // An open code on the server is always shown (also when re-pairing to another chat). A code made
  // here but no longer on the server was used, unless the server cannot show codes (no Redis): then
  // it is shown until the owner is paired.
  const open = pairing !== null && (s.pairing !== null || !s.owner_paired);
  const now = useNow(1000, open);
  const left = pairing ? secondsLeft(pairing.expires_at, now) : 0;
  const showCode = open && left > 0;
  const bot = s.bot;

  const create = () => {
    setBusy(true);
    api
      .createTelegramPairing()
      .then((p) => {
        onCreated(p);
        void qc.invalidateQueries({ queryKey: keys.telegram });
      })
      .catch((e: unknown) => toast.error("کد اتصال ساخته نشد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const testAlert = () => {
    setTesting(true);
    api
      .sendTelegramTestAlert()
      .then(() => toast.success("هشدار آزمایشی در صف ارسال است", "تا چند ثانیه‌ی دیگر در چت تلگرام شما میرسد."))
      .catch((e: unknown) => toast.error("هشدار آزمایشی فرستاده نشد", errorMessage(e)))
      .finally(() => setTesting(false));
  };

  const deep = bot && pairing ? pairingLink(bot.username, pairing.code) : null;

  return (
    <Card className="lg:col-span-7">
      <CardHeader
        icon={<Smartphone className="size-5" />}
        title="اتصال چت شما (هشدارها)"
        subtitle="هشدارهای پنل در چت خصوصی شما با ربات میرسد. یک بار وصل میکنید."
        action={s.owner_paired ? <Badge tone="success" dot>وصل</Badge> : <Badge tone="warning">وصل نیست</Badge>}
      />
      <div className="flex flex-col gap-4 p-5">
        {!bot ? (
          <p className="rounded-2xl bg-surface-2 p-3.5 text-sm leading-7 text-fg-muted">اول توکن ربات را وارد کنید؛ بعد اینجا کد اتصال ساخته میشود.</p>
        ) : s.owner_paired && !showCode ? (
          <>
            <div className="flex items-center gap-3 rounded-2xl border border-success/25 bg-success-soft p-3.5">
              <CircleCheck className="size-6 shrink-0 text-success" />
              <p className="text-sm leading-7 text-fg">
                چت شما وصل است. هشدارها و پیام‌های پنل به تلگرام شما میرسد.
                <span className="block text-xs text-fg-muted">
                  آخرین هشدار: {s.last_alert_at ? `${formatRelative(s.last_alert_at)} · ${formatJalali(s.last_alert_at, "short")}` : "هنوز هیچ"}
                </span>
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button icon={<BellRing className="size-4" />} loading={testing} onClick={testAlert}>
                فرستادن هشدار آزمایشی
              </Button>
              <Button variant="ghost" size="md" loading={busy} onClick={create}>
                وصل کردن چت دیگر
              </Button>
            </div>
          </>
        ) : showCode ? (
          <>
            <div className="flex flex-col items-center gap-2 rounded-3xl border border-brand/25 bg-brand-soft p-4 text-center">
              <span className="text-xs font-semibold text-fg-muted">کد اتصال</span>
              <code data-ph-secret className="ltr text-3xl font-black tracking-[0.3em] text-fg sm:text-4xl">
                {pairing.code}
              </code>
              <span className={cn("num text-sm font-bold", left < 60 ? "text-danger" : "text-fg-muted")} aria-live="polite">
                {formatCountdown(left)} مانده
              </span>
            </div>
            <ol className="flex flex-col gap-3">
              <li className="flex gap-3">
                <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-brand text-sm font-bold text-white">۱</span>
                <div className="min-w-0 flex-1 text-sm leading-7 text-fg">
                  ربات را در تلگرام باز کنید و <b>Start</b> را بزنید:
                  {deep ? (
                    <a
                      href={deep}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-1.5 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-[linear-gradient(110deg,var(--ph-brand),var(--ph-brand-2))] font-bold text-white sm:w-auto sm:px-5"
                    >
                      <Send className="size-4 rtl:-scale-x-100" />
                      باز کردن ‎@{bot.username}‎
                    </a>
                  ) : null}
                </div>
              </li>
              <li className="flex gap-3">
                <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-brand text-sm font-bold text-white">۲</span>
                <div className="min-w-0 flex-1 text-sm leading-7 text-fg">
                  اگر پیام خودکار نرفت، همین را برای ربات بفرستید:
                  <div className="mt-1.5 flex items-center gap-2">
                    <code data-ph-secret className="ltr min-w-0 flex-1 truncate rounded-xl bg-surface-3 px-3 py-2 text-left font-mono text-sm">
                      {startCommand(pairing.code)}
                    </code>
                    <CopyButton text={startCommand(pairing.code)} />
                  </div>
                </div>
              </li>
              <li className="flex gap-3">
                <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-brand text-sm font-bold text-white">۳</span>
                <p className="flex-1 text-sm leading-7 text-fg">
                  همین صفحه خودش میفهمد؛ چند ثانیه بعد «وصل» میشود. کد فقط ۱۰ دقیقه و یک بار کار میکند.
                </p>
              </li>
            </ol>
            <div className="flex items-center gap-2 text-xs text-fg-muted">
              <StatusDot tone="info" pulse />
              در انتظار پیام شما به ربات…
            </div>
          </>
        ) : (
          <>
            <p className="text-sm leading-7 text-fg-muted">
              یک کد ۸ حرفی ساخته میشود. آن را با <code className="ltr inline-block rounded bg-surface-3 px-1">/start</code> برای ربات میفرستید
              تا پنل چت شما را بشناسد. {pairing && left === 0 ? <b className="text-danger">کد قبلی منقضی شد.</b> : null}
            </p>
            <Button className="self-start" icon={<Link2 className="size-4" />} loading={busy} onClick={create}>
              {pairing ? "ساخت کد تازه" : "ساخت کد اتصال"}
            </Button>
          </>
        )}
      </div>
    </Card>
  );
}

// --- channels ---------------------------------------------------------------------------------------

function Permission({ ok, label, invert = false }: { ok: boolean; label: string; invert?: boolean }) {
  const good = invert ? !ok : ok;
  const tone: Tone = good ? "success" : "danger";
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold", TONE_SOFT[tone])}>
      {good ? <CircleCheck className="size-3.5" /> : <CircleX className="size-3.5" />}
      {label}
    </span>
  );
}

function ChannelForm({ role, channel, botSet, index }: { role: ChannelRole; channel: TgChannel | null; botSet: boolean; index: number }) {
  const qc = useQueryClient();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = `tg-channel-${role}`;

  const check = (raw: string) => {
    const parsed = normalizeChannel(raw);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setBusy(true);
    setError(null);
    api
      .putTelegramChannel(role, parsed.value)
      .then((status) => {
        qc.setQueryData(keys.telegram, status);
        void qc.invalidateQueries({ queryKey: ["events"] });
        setValue("");
        const ch = status.channels[role];
        if (ch?.can_delete) toast.warning("کانال ثبت شد، ولی ربات اجازه‌ی حذف دارد", "این اجازه را در تنظیمات ادمین کانال بردارید.");
        else toast.success("کانال ثبت شد", ch ? `${ch.title || ch.username}: ارسال و ویرایش درست است.` : undefined);
      })
      .catch((e: unknown) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const testPost = () => {
    setPosting(true);
    api
      .sendTelegramTestPost()
      .then((r) => {
        void qc.invalidateQueries({ queryKey: keys.telegram });
        toast.success("پست آزمایشی در صف ارسال است", `فقط در کانال آزمایشی میرود (صف شماره‌ی ${formatNumber(r.outbox_id)}).`);
      })
      .catch((e: unknown) => toast.error("پست آزمایشی فرستاده نشد", errorMessage(e)))
      .finally(() => setPosting(false));
  };

  const title = role === "main" ? "کانال اصلی" : "کانال آزمایشی";
  const sub =
    role === "main"
      ? "پست‌های پروکسی اینجا میروند (مثل ‎@ipoxxo‎). پست‌ها هرگز پاک نمیشوند، فقط ویرایش."
      : "برای آزمودن پست‌ها پیش از کانال اصلی. پست آزمایشی فقط اینجا میرود.";

  return (
    <motion.div {...fadeUp(index)} className="flex flex-col gap-3 rounded-3xl border border-line bg-surface-2/50 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="flex items-center gap-1.5 text-base font-bold text-fg">
            {role === "main" ? <Megaphone className="size-4 text-brand" /> : <Send className="size-4 text-brand rtl:-scale-x-100" />}
            {title}
          </h3>
          <p className="text-[0.82rem] leading-6 text-fg-muted">{sub}</p>
        </div>
        {channel ? null : <Badge tone="warning">تعیین نشده</Badge>}
      </div>

      {channel ? (
        <div className="flex flex-col gap-2.5 rounded-2xl border border-line bg-surface p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-bold text-fg">{channel.title || "بی عنوان"}</div>
              <div className="ltr truncate text-right text-xs text-fg-muted">{channel.username}</div>
            </div>
            <span className="shrink-0 text-xs text-fg-faint" title={formatJalali(channel.checked_at, "datetime")}>
              وارسی {formatRelative(channel.checked_at)}
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Permission ok={channel.can_post} label="ارسال پیام" />
            <Permission ok={channel.can_edit} label="ویرایش پیام" />
            <Permission ok={channel.can_delete} label={channel.can_delete ? "اجازه‌ی حذف دارد" : "بی اجازه‌ی حذف"} invert />
          </div>
          {channel.can_delete ? (
            <p role="alert" className="flex gap-2 rounded-xl border border-danger/30 bg-danger-soft p-3 text-[0.84rem] leading-7 text-fg">
              <ShieldAlert className="mt-1 size-4 shrink-0 text-danger" />
              <span>
                <b className="text-danger">ربات نباید اجازه‌ی حذف پیام داشته باشد.</b> پست‌های کانال هرگز پاک نمیشوند. در تلگرام، در تنظیمات
                ادمین‌های کانال، «حذف پیام‌ها» را برای ربات خاموش کنید و بعد «وارسی دوباره» را بزنید.
              </span>
            </p>
          ) : null}
        </div>
      ) : null}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy) check(value);
        }}
        noValidate
        className="flex flex-col gap-2"
      >
        <Field
          label={channel ? "کانال دیگری؟" : "نام کانال"}
          htmlFor={id}
          error={error}
          hint="ربات را ادمین کانال کنید: «ارسال پیام» و «ویرایش پیام‌های دیگران» روشن، «حذف پیام‌ها» خاموش."
        >
          <TextInput
            id={id}
            latin
            value={value}
            onChange={(e) => setValue(e.target.value)}
            invalid={Boolean(error)}
            placeholder={role === "main" ? "@ipoxxo" : "@my_test_channel"}
            autoCapitalize="none"
            spellCheck={false}
            disabled={!botSet}
          />
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" loading={busy} disabled={!botSet || value.trim() === ""}>
            ثبت و وارسی
          </Button>
          {channel ? (
            <Button variant="secondary" size="sm" disabled={!botSet || busy} onClick={() => check(channel.username)}>
              وارسی دوباره
            </Button>
          ) : null}
          {role === "test" ? (
            <Button variant="secondary" size="sm" icon={<Send className="size-4 rtl:-scale-x-100" />} loading={posting} disabled={!channel || !botSet} onClick={testPost}>
              ارسال پست آزمایشی
            </Button>
          ) : null}
        </div>
        {!botSet ? <p className="text-xs text-fg-faint">اول توکن ربات را وارد کنید.</p> : null}
      </form>
    </motion.div>
  );
}

function OutboxCard({ s }: { s: TgStatus }) {
  return (
    <Card className="lg:col-span-5">
      <CardHeader icon={<Inbox className="size-5" />} title="صف ارسال" subtitle="پست‌ها و هشدارها از صف، با رعایت سقف سرعت تلگرام فرستاده میشوند." />
      <div className="grid grid-cols-2 gap-3 p-5">
        <div className="rounded-2xl border border-line bg-surface-2 p-3">
          <div className="text-xs text-fg-muted">در صف</div>
          <div className="num mt-1 text-3xl font-extrabold text-fg">{formatNumber(s.outbox.pending)}</div>
        </div>
        <div className={cn("rounded-2xl border p-3", s.outbox.failed > 0 ? "border-danger/30 bg-danger-soft" : "border-line bg-surface-2")}>
          <div className="text-xs text-fg-muted">ناموفق</div>
          <div className={cn("num mt-1 text-3xl font-extrabold", s.outbox.failed > 0 ? "text-danger" : "text-fg")}>{formatNumber(s.outbox.failed)}</div>
        </div>
        <p className="col-span-2 text-xs leading-6 text-fg-muted">
          آخرین هشدار به شما: {s.last_alert_at ? `${formatRelative(s.last_alert_at)} · ${formatJalali(s.last_alert_at, "short")}` : "هنوز هیچ"}
        </p>
      </div>
    </Card>
  );
}

// --- no-post windows and interval (decision D21) ---------------------------------------------------------

interface DraftWindow extends QuietWindow {
  key: number;
}

const HOURS = [0, 6, 12, 18, 24];

/** 24 h of Tehran time; right edge is 00:00 (RTL), shaded parts are no-post windows. */
function DayTimeline({ windows, now }: { windows: QuietWindow[]; now: Date }) {
  const minute = tehranMinutes(now);
  return (
    <div className="pt-1 pb-5" aria-hidden>
      <div className="relative h-7 overflow-clip rounded-lg bg-success-soft ring-1 ring-line">
        {windows.flatMap((w, wi) =>
          segments(w).map((seg, si) => (
            <motion.div
              key={`${String(wi)}-${String(si)}`}
              initial={{ opacity: 0, scaleX: 0.6 }}
              animate={{ opacity: 1, scaleX: 1 }}
              className="absolute inset-y-0 bg-[repeating-linear-gradient(135deg,var(--ph-warning)_0_6px,color-mix(in_srgb,var(--ph-warning)_70%,transparent)_6px_12px)]"
              style={{ insetInlineStart: `${String((seg.from / 1440) * 100)}%`, width: `${String(((seg.to - seg.from) / 1440) * 100)}%` }}
            />
          )),
        )}
        <div className="absolute inset-y-0 w-0.5 bg-fg" style={{ insetInlineStart: `${String((minute / 1440) * 100)}%` }} />
      </div>
      <div className="relative mt-1 h-4 text-[0.68rem] text-fg-faint">
        {HOURS.map((h) => (
          <span
            key={h}
            className="absolute -translate-x-1/2 rtl:translate-x-1/2"
            style={{ insetInlineStart: `${String((h / 24) * 100)}%` }}
          >
            {toFaDigits(String(h).padStart(2, "0"))}
          </span>
        ))}
      </div>
    </div>
  );
}

function ScheduleForm({ initial, onSaved }: { initial: TgSettings; onSaved: () => void }) {
  const qc = useQueryClient();
  const nextKey = useRef(initial.quiet_windows.length + 1);
  const [windows, setWindows] = useState<DraftWindow[]>(() => initial.quiet_windows.map((w, i) => ({ ...w, key: i + 1 })));
  const [interval, setIntervalText] = useState(String(initial.post_interval_min));
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const now = useNow(30_000);

  const plain = windows.map(({ start, end }) => ({ start, end }));
  const { settings, errors } = validateSchedule(plain, interval);
  const dirty = !settings || !sameSchedule(settings, initial);
  // What is in effect now is the SAVED schedule, not the draft being edited.
  const current = activeWindow(initial.quiet_windows, now);

  const update = (key: number, patch: Partial<QuietWindow>) => {
    setWindows((ws) => ws.map((w) => (w.key === key ? { ...w, ...patch } : w)));
    setServerError(null);
  };

  const save = (e?: SubmitEvent) => {
    e?.preventDefault();
    setSubmitted(true);
    if (!settings || busy) return;
    setBusy(true);
    setServerError(null);
    api
      .putTelegramSettings(settings)
      .then((saved) => {
        qc.setQueryData(keys.telegramSettings, saved);
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("زمان‌بندی ذخیره شد", `ساعت ${formatJalali(new Date(), "time")}`);
        onSaved();
      })
      .catch((err: unknown) => {
        setServerError(errorMessage(err));
        toast.error("زمان‌بندی ذخیره نشد", errorMessage(err));
      })
      .finally(() => setBusy(false));
  };

  const shownWindowError = (i: number) => (submitted ? (errors.windows[i] ?? null) : null);

  return (
    <form onSubmit={save} noValidate className="flex flex-col gap-4 p-5">
      <div className="flex flex-wrap items-center gap-2">
        {current ? (
          <Badge tone="warning" dot pulse>
            الان در بازه‌ی بی‌پست (تا {faTime(current.end)})
          </Badge>
        ) : (
          <Badge tone="success" dot>
            الان پست تازه مجاز است
          </Badge>
        )}
        <span className="text-xs text-fg-muted">ساعت تهران: {formatJalali(now, "time").slice(0, 5)}</span>
      </div>

      <DayTimeline windows={settings?.quiet_windows ?? plain.filter((_, i) => errors.windows[i] === null)} now={now} />

      <ul className="flex flex-col gap-2.5">
        <AnimatePresence initial={false}>
          {windows.map((w, i) => {
            const err = shownWindowError(i);
            return (
              <motion.li
                key={w.key}
                layout
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, height: 0 }}
                className={cn("rounded-2xl border bg-surface-2/60 p-3", err ? "border-danger/40" : "border-line")}
              >
                <div className="flex flex-wrap items-end gap-2.5">
                  <div className="flex flex-col gap-1">
                    <span className="text-xs font-semibold text-fg-muted">از</span>
                    <TimeSelect value={w.start} onChange={(v) => update(w.key, { start: v })} label={`آغاز بازه‌ی ${toFaDigits(i + 1)}`} invalid={Boolean(err)} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <span className="text-xs font-semibold text-fg-muted">تا</span>
                    <TimeSelect value={w.end} onChange={(v) => update(w.key, { end: v })} label={`پایان بازه‌ی ${toFaDigits(i + 1)}`} invalid={Boolean(err)} />
                  </div>
                  {/* Phone: length and badge on their own line under the times; wider: between them and the button. */}
                  <div className="order-last flex w-full flex-wrap items-center gap-1.5 sm:order-none sm:min-h-11 sm:w-auto sm:flex-1">
                    {errors.windows[i] === null ? <span className="text-sm font-semibold text-fg">{windowLengthText(w)}</span> : null}
                    {crossesMidnight(w) ? <Badge tone="info">از نیمه‌شب میگذرد</Badge> : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      setWindows((ws) => ws.filter((x) => x.key !== w.key));
                      setServerError(null);
                    }}
                    className="ms-auto grid size-11 place-items-center rounded-xl text-fg-faint hover:bg-danger-soft hover:text-danger sm:ms-0"
                    aria-label={`حذف بازه‌ی ${toFaDigits(i + 1)}`}
                    title="برداشتن این بازه"
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>
                {err ? (
                  <p role="alert" className="mt-1.5 text-[0.82rem] leading-6 text-danger">
                    {err}
                  </p>
                ) : null}
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ul>
      {windows.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-line-strong p-4 text-center text-sm text-fg-muted">
          هیچ بازه‌ی بی‌پستی نیست؛ پست‌ها در همه‌ی ساعت‌ها مجازند.
        </p>
      ) : null}
      <Button
        variant="secondary"
        size="sm"
        className="self-start"
        icon={<Plus className="size-4" />}
        disabled={windows.length >= MAX_WINDOWS}
        onClick={() => {
          setWindows((ws) => [...ws, { key: nextKey.current++, start: "20:00", end: "22:00" }]);
          setServerError(null);
        }}
      >
        افزودن بازه
      </Button>
      <AnimatePresence>{submitted && errors.form ? <ErrorNote>{errors.form}</ErrorNote> : null}</AnimatePresence>

      <div className="border-t border-line pt-4 sm:max-w-sm">
        <Field
          label="کمترین فاصله‌ی دو پست"
          htmlFor="tg-interval"
          hint={`به دقیقه، ۰ تا ${toFaDigits(MAX_POST_INTERVAL_MIN)}؛ برای پست‌های تازه‌ی کانال اصلی. ۰ یعنی بی فاصله‌ی اجباری.`}
          error={errors.interval && (submitted || interval !== String(initial.post_interval_min)) ? errors.interval : null}
        >
          <TextInput
            id="tg-interval"
            inputMode="numeric"
            latin
            value={interval}
            onChange={(e) => {
              setIntervalText(e.target.value);
              setServerError(null);
            }}
            invalid={Boolean(errors.interval)}
            suffix="دقیقه"
            className="pl-16"
          />
        </Field>
      </div>
      <AnimatePresence>{serverError ? <ErrorNote>{serverError}</ErrorNote> : null}</AnimatePresence>

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
                  setWindows(initial.quiet_windows.map((w, i) => ({ ...w, key: i + 1 })));
                  setIntervalText(String(initial.post_interval_min));
                  setSubmitted(false);
                  setServerError(null);
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
    </form>
  );
}

function ScheduleCard() {
  const settings = useTelegramSettings();
  const [version, setVersion] = useState(0);
  return (
    <Card className="lg:col-span-12">
      <CardHeader
        icon={<MoonStar className="size-5" />}
        title="بازه‌های بی‌پست و فاصله‌ی پست‌ها"
        subtitle="در این ساعت‌ها (به وقت تهران) هیچ پست تازه‌ای در کانال اصلی نمیرود؛ مثلاً هنگام تبلیغ فروخته‌شده. ویرایش پست‌های قبلی و هشدارها ادامه دارد."
      />
      {settings.isPending ? (
        <div className="p-5">
          <SkeletonLines lines={4} />
        </div>
      ) : settings.isError ? (
        <LoadError error={settings.error} compact onRetry={() => void settings.refetch()} />
      ) : (
        <ScheduleForm key={version} initial={settings.data} onSaved={() => setVersion((v) => v + 1)} />
      )}
    </Card>
  );
}

// --- page ---------------------------------------------------------------------------------------------

export default function TelegramPage() {
  const [created, setCreated] = useState<TgPairing | null>(null);
  // Polls faster only while a pairing code waits for the owner's /start.
  const status = useTelegramStatus(created !== null);
  const s = status.data;

  // Tell the owner as soon as the bot has seen their /start.
  const wasPaired = useRef<boolean | null>(null);
  useEffect(() => {
    if (!s) return;
    if (wasPaired.current === false && s.owner_paired) toast.success("چت شما به ربات وصل شد", "از این پس هشدارها در تلگرام شما میرسد.");
    wasPaired.current = s.owner_paired;
  }, [s]);

  return (
    <>
      <PageHeader
        title="تلگرام"
        subtitle="ربات پست‌گذار، اتصال چت شما برای هشدار، حساب عملیاتی ‎@MTProxybot‎، کانال‌ها و زمان‌بندی پست. توکن‌ها و رمزها پس از ذخیره هرگز نشان داده نمیشوند."
      />
      {status.isPending ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-28 rounded-3xl" />
          <div className="grid gap-4 lg:grid-cols-12">
            <Skeleton className="h-72 rounded-3xl lg:col-span-5" />
            <Skeleton className="h-72 rounded-3xl lg:col-span-7" />
          </div>
        </div>
      ) : status.isError ? (
        <Card>
          <LoadError
            error={status.error}
            onRetry={() => void status.refetch()}
            notYet={{
              title: "بخش تلگرام هنوز روی پنل فعال نشده",
              description:
                "ثبت ربات، اتصال چت شما برای هشدار، کانال‌های اصلی و آزمایشی و بازه‌های بی‌پست با به‌روزرسانی بعدی پنل فعال میشوند. کاری از شما لازم نیست.",
            }}
          />
        </Card>
      ) : s ? (
        <div className="flex flex-col gap-4 sm:gap-5">
          <SetupSteps s={s} />
          <div className="grid items-start gap-4 sm:gap-5 lg:grid-cols-12">
            <BotCard s={s} />
            <PairingCard s={s} created={created} onCreated={setCreated} />
          </div>
          <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
            <MtproxyAccountCard />
          </div>
          <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
            <Card className="lg:col-span-7">
              <CardHeader icon={<Megaphone className="size-5" />} title="کانال‌ها" subtitle="ربات در هر دو کانال ادمین با «ارسال» و «ویرایش»، بی «حذف»." />
              <div className="grid gap-3 p-4 sm:p-5 xl:grid-cols-2">
                <ChannelForm role="main" channel={s.channels.main} botSet={s.bot !== null} index={0} />
                <ChannelForm role="test" channel={s.channels.test} botSet={s.bot !== null} index={1} />
              </div>
            </Card>
            <OutboxCard s={s} />
          </div>
          <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
            <ScheduleCard />
          </div>
        </div>
      ) : null}
    </>
  );
}
