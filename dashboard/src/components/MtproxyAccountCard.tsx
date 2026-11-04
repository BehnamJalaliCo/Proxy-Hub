import { useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  CircleAlert,
  CircleCheck,
  Info,
  KeyRound,
  LockKeyhole,
  MessageSquareLock,
  Send,
  ShieldAlert,
  Unplug,
  UserRoundCheck,
  UserRoundCog,
  X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { Button } from "./ui/Button";
import { Badge, Card, CardHeader } from "./ui/Card";
import { ConfirmDialog } from "./ui/Dialogs";
import { LoadError, SkeletonLines } from "./ui/Feedback";
import { Field, SecretInput, TextInput } from "./ui/Form";
import { errorMessage, isApiError } from "../lib/api";
import { cn } from "../lib/browser";
import * as api from "../lib/endpoints";
import { formatJalali, formatRelative, toFaDigits } from "../lib/format";
import {
  apiHashProblem,
  apiIdProblem,
  canStartLogin,
  checkErrorText,
  codeViaText,
  disconnectReasonText,
  isPending,
  mtpStateInfo,
  normalizeCode,
  normalizePhone,
  passwordProblem,
} from "../lib/mtproxy";
import { keys, useMtproxyAccount } from "../lib/queries";
import { formatCountdown, secondsLeft } from "../lib/telegram";
import { toast } from "../lib/toast";
import type { MtpAccount } from "../lib/types";
import { useNow } from "../lib/useNow";

const BOT = "‎@MTProxybot‎";

function Note({ tone, icon, children }: { tone: "danger" | "warning" | "info" | "success"; icon?: ReactNode; children: ReactNode }) {
  const style = {
    danger: "bg-danger-soft text-danger",
    warning: "bg-warning-soft text-warning",
    info: "bg-info-soft text-info",
    success: "bg-success-soft text-success",
  }[tone];
  return (
    <motion.div
      role={tone === "danger" ? "alert" : undefined}
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className={cn("flex gap-2.5 rounded-2xl p-3.5 text-sm leading-7", style)}
    >
      <span className="mt-1 shrink-0">{icon ?? <CircleAlert className="size-4" />}</span>
      <div className="min-w-0 flex-1 text-fg">{children}</div>
    </motion.div>
  );
}

/** A Latin name from Telegram's own English UI, kept whole and left-to-right inside Persian text, in
 * bold instead of guillemets (which render the wrong way around a left-to-right run). */
function Latin({ children }: { children: ReactNode }) {
  return <b className="ltr inline-block whitespace-nowrap">{children}</b>;
}

/** Which input an error code belongs to (shown under that field); null: shown above the buttons. */
function fieldOf(code: string): "api_id" | "api_hash" | "phone" | "code" | "password" | null {
  if (code === "api_id_format") return "api_id";
  if (code === "api_hash_format") return "api_hash";
  if (code.startsWith("phone_number") || code === "phone_format") return "phone";
  if (code === "code_format" || code === "phone_code_invalid") return "code";
  if (code === "password_format" || code === "password_invalid") return "password";
  return null;
}

interface FormError {
  field: ReturnType<typeof fieldOf>;
  text: string;
}

function toFormError(err: unknown): FormError {
  const code = isApiError(err) ? err.code : "";
  const left = isApiError(err) ? err.num("attempts_left") : null;
  const extra = left !== null ? ` (${toFaDigits(left)} بار دیگر میتوانید امتحان کنید.)` : "";
  return { field: fieldOf(code), text: errorMessage(err) + extra };
}

// --- what the owner must know first ---------------------------------------------------------------

function Rules() {
  return (
    <div className="flex flex-col gap-3">
      <Note tone="warning" icon={<ShieldAlert className="size-4" />}>
        <b>فقط حساب دوم تلگرام، هرگز حساب شخصی شما.</b> (تصمیم D37) یک حساب جدا با سیم‌کارت واقعی که نگه میدارید،
        ساخته‌شده با برنامه‌ی رسمی تلگرام و با رمز دومرحله‌ای. api_id و api_hash را هم با <b>همین حساب دوم</b> در
        my.telegram.org بسازید. اگر دکمه‌ی <Latin>Set promotion</Latin> ادمین بودن را بخواهد، همین حساب با کمترین اجازه ادمین ‎@ipoxxo‎ میشود.
      </Note>
      <ul className="flex flex-col gap-2 text-sm leading-7 text-fg">
        <li className="flex gap-2">
          <CircleCheck className="mt-1.5 size-4 shrink-0 text-success" />
          <span>پنل با این حساب فقط با {BOT} کار خواهد کرد. در این نسخه حتی به آن هم پیامی نمیدهد: فقط وارد میشود، نشست را میسنجد و خارج میشود.</span>
        </li>
        <li className="flex gap-2">
          <Ban className="mt-1.5 size-4 shrink-0 text-danger" />
          <span>فهرست چت‌ها، مخاطب‌ها یا پیام هیچ گفتگوی دیگری را <b>هرگز</b> نمیخواند و به کسی پیام نمیدهد.</span>
        </li>
        <li className="flex gap-2">
          <LockKeyhole className="mt-1.5 size-4 shrink-0 text-brand" />
          <span>api_hash و نشست رمزنگاری‌شده در گاوصندوق پنل میمانند و هرگز نشان داده نمیشوند. کد ورود و رمز دومرحله‌ای یک بار به کار میروند و جایی نمیمانند.</span>
        </li>
        <li className="flex gap-2">
          <Info className="mt-1.5 size-4 shrink-0 text-info" />
          <span>
            هر لحظه میتوانید در اپ تلگرام حساب دوم از <b>Settings ← Devices</b> نشست <Latin>Proxy-Hub panel</Latin> را ببندید؛ پنل خودش میفهمد و به شما خبر میدهد.
          </span>
        </li>
      </ul>
    </div>
  );
}

// --- step 1: api_id, api_hash, phone -----------------------------------------------------------------

function StartForm({ onDone }: { onDone: (s: MtpAccount) => void }) {
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [phone, setPhone] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<FormError | null>(null);

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const idProblem = apiIdProblem(apiId);
    const hashProblem = apiHashProblem(apiHash);
    const parsedPhone = normalizePhone(phone);
    if (idProblem) {
      setError({ field: "api_id", text: idProblem });
      return;
    }
    if (hashProblem) {
      setError({ field: "api_hash", text: hashProblem });
      return;
    }
    if (!parsedPhone.ok) {
      setError({ field: "phone", text: parsedPhone.error });
      return;
    }
    setBusy(true);
    setError(null);
    api
      .startMtproxyLogin(apiId.trim(), apiHash.trim(), parsedPhone.value)
      .then((s) => {
        setApiId("");
        setPhone("");
        onDone(s);
        toast.success("کد ورود فرستاده شد", "کد را در اپ تلگرام حساب دوم ببینید و اینجا وارد کنید.");
      })
      .catch((err: unknown) => setError(toFormError(err)))
      .finally(() => {
        // The api_hash is never kept for another try, accepted or not.
        setApiHash("");
        setBusy(false);
      });
  };

  return (
    <form onSubmit={submit} noValidate autoComplete="off" className="flex flex-col gap-3">
      <ol className="flex list-inside list-[persian] flex-col gap-1 text-[0.84rem] leading-7 text-fg-muted marker:font-bold marker:text-brand">
        <li>
          با <b>حساب دوم</b> به{" "}
          <a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer" className="font-bold text-brand hover:underline">
            my.telegram.org
          </a>{" "}
          بروید و در API development tools یک برنامه بسازید.
        </li>
        <li>«App api_id» و «App api_hash» را اینجا بچسبانید، با شماره‌ی همان حساب دوم.</li>
        <li>تلگرام یک کد به اپ حساب دوم میفرستد؛ در گام بعد همان را وارد میکنید.</li>
      </ol>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="api_id" htmlFor="mtp-api-id" error={error?.field === "api_id" ? error.text : null}>
          <TextInput
            id="mtp-api-id"
            name="mtproxy-api-id"
            latin
            inputMode="numeric"
            autoComplete="off"
            value={apiId}
            onChange={(e) => setApiId(e.target.value)}
            invalid={error?.field === "api_id"}
            placeholder="1234567"
          />
        </Field>
        <Field label="شماره‌ی حساب دوم" htmlFor="mtp-phone" hint="با + و کد کشور" error={error?.field === "phone" ? error.text : null}>
          <TextInput
            id="mtp-phone"
            name="mtproxy-phone"
            latin
            type="tel"
            inputMode="tel"
            autoComplete="off"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            invalid={error?.field === "phone"}
            placeholder="+44 …"
          />
        </Field>
      </div>
      <Field
        label="api_hash"
        htmlFor="mtp-api-hash"
        hint="پس از فرستادن، api_hash هرگز دوباره نشان داده نمیشود."
        error={error?.field === "api_hash" ? error.text : null}
      >
        <SecretInput
          id="mtp-api-hash"
          name="mtproxy-api-hash"
          autoComplete="off"
          data-1p-ignore
          data-lpignore="true"
          allowReveal={false}
          value={apiHash}
          onChange={(e) => setApiHash(e.target.value)}
          invalid={error?.field === "api_hash"}
          placeholder="••••••••••••••••••••••••••••••••"
        />
      </Field>
      <AnimatePresence>{error && error.field === null ? <Note tone="danger">{error.text}</Note> : null}</AnimatePresence>
      <Button type="submit" loading={busy} icon={<Send className="size-4 rtl:-scale-x-100" />} className="sm:self-start">
        فرستادن کد ورود
      </Button>
    </form>
  );
}

// --- steps 2 and 3: the code, then (maybe) the two-step password -----------------------------------------

function StepForm({ s, onDone }: { s: MtpAccount; onDone: (s: MtpAccount) => void }) {
  const isCode = s.state === "code_sent";
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<FormError | null>(null);
  const now = useNow(1000);
  const left = s.login?.expires_at ? secondsLeft(s.login.expires_at, now) : 0;
  const id = isCode ? "mtp-code" : "mtp-password";

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    let call: Promise<MtpAccount>;
    if (isCode) {
      const code = normalizeCode(value);
      if (!code.ok) {
        setError({ field: "code", text: code.error });
        return;
      }
      call = api.sendMtproxyCode(code.value);
    } else {
      const problem = passwordProblem(value);
      if (problem) {
        setError({ field: "password", text: problem });
        return;
      }
      call = api.sendMtproxyPassword(value);
    }
    setBusy(true);
    setError(null);
    call
      .then((next) => {
        onDone(next);
        if (next.state === "connected") toast.success("حساب عملیاتی وصل شد", next.account ? next.account.first_name : undefined);
        else if (next.state === "password_needed") toast.info("رمز دومرحله‌ای لازم است", "این حساب رمز دومرحله‌ای دارد؛ آن را وارد کنید.");
      })
      .catch((err: unknown) => setError(toFormError(err)))
      .finally(() => {
        // Neither the code nor the password is kept for another try.
        setValue("");
        setBusy(false);
      });
  };

  const cancel = () => {
    setCancelling(true);
    api
      .logoutMtproxyAccount()
      .then(onDone)
      .catch((err: unknown) => toast.error("لغو نشد", errorMessage(err)))
      .finally(() => setCancelling(false));
  };

  return (
    <form onSubmit={submit} noValidate autoComplete="off" className="flex flex-col gap-3">
      <div className="flex flex-col gap-1 rounded-2xl border border-brand/25 bg-brand-soft p-3.5 text-sm leading-7 text-fg">
        {isCode ? (
          <span className="flex items-start gap-2">
            <MessageSquareLock className="mt-1.5 size-4 shrink-0 text-brand" />
            <span>
              کد ورود {s.login?.code_length ? `${toFaDigits(s.login.code_length)} رقمی ` : ""}
              {codeViaText(s.login?.code_via)} این کد را به هیچ کس دیگری ندهید.
            </span>
          </span>
        ) : (
          <span className="flex items-start gap-2">
            <KeyRound className="mt-1.5 size-4 shrink-0 text-brand" />
            <span>کد درست بود. این حساب رمز دومرحله‌ای دارد؛ آن را وارد کنید. پنل آن را فقط همین یک بار به کار میبرد و نگه نمیدارد.</span>
          </span>
        )}
        <span className="text-xs text-fg-muted">
          شماره: <span className="ltr inline-block">{s.phone_masked ?? "—"}</span> ·{" "}
          <span className={cn("num font-bold", left < 60 ? "text-danger" : "")} aria-live="polite">
            {formatCountdown(left)} مانده
          </span>
        </span>
      </div>
      <Field label={isCode ? "کد ورود" : "رمز دومرحله‌ای حساب دوم"} htmlFor={id} error={error && error.field !== null ? error.text : null}>
        {isCode ? (
          <TextInput
            id={id}
            name="mtproxy-login-code"
            latin
            inputMode="numeric"
            autoComplete="one-time-code"
            className="text-center text-xl tracking-[0.4em]"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            invalid={error?.field === "code"}
            placeholder="•••••"
          />
        ) : (
          <SecretInput
            id={id}
            name="mtproxy-two-step"
            autoComplete="off"
            data-1p-ignore
            data-lpignore="true"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            invalid={error?.field === "password"}
          />
        )}
      </Field>
      <AnimatePresence>{error && error.field === null ? <Note tone="danger">{error.text}</Note> : null}</AnimatePresence>
      <div className="flex flex-col-reverse gap-2 sm:flex-row">
        <Button variant="secondary" icon={<X className="size-4" />} loading={cancelling} disabled={busy} onClick={cancel}>
          لغو ورود
        </Button>
        <Button type="submit" className="sm:flex-1" loading={busy} disabled={cancelling} icon={<UserRoundCheck className="size-5" />}>
          ورود
        </Button>
      </div>
    </form>
  );
}

// --- connected / ended / broken --------------------------------------------------------------------

function AccountBox({ s }: { s: MtpAccount }) {
  const name = s.account?.first_name.trim() ?? "";
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface-2 p-3">
      <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">
        <UserRoundCog className="size-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate font-bold text-fg">{name !== "" ? name : "حساب دوم"}</div>
        <div className="flex flex-wrap gap-x-3 text-xs text-fg-muted">
          <span className="ltr inline-block">{s.phone_masked ?? "—"}</span>
          {s.account ? (
            <span>
              شناسه‌ی تلگرام <span className="ltr inline-block">{s.account.user_id}</span>
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function SessionFacts({ s }: { s: MtpAccount }) {
  const rows: [string, ReactNode][] = [
    ["وصل از", s.connected_at ? `${formatRelative(s.connected_at)} · ${formatJalali(s.connected_at, "short")}` : "—"],
    ["آخرین بررسی", s.last_check_at ? formatRelative(s.last_check_at) : "—"],
    [BOT, s.mtproxybot_ok ? <span className="font-semibold text-success">دیده میشود</span> : <span className="font-semibold text-warning">هنوز دیده نشده</span>],
  ];
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 text-sm sm:grid-cols-3">
      {rows.map(([k, v]) => (
        <div key={k} className="flex justify-between gap-2 rounded-xl bg-surface-2 px-3 py-2 sm:flex-col sm:justify-start">
          <dt className="text-xs text-fg-muted">{k}</dt>
          <dd className="text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function Connected({ s, onDone }: { s: MtpAccount; onDone: (s: MtpAccount) => void }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const warning = checkErrorText(s.state === "error" ? s.error : s.last_check_error);

  const logout = () => {
    setBusy(true);
    api
      .logoutMtproxyAccount()
      .then((next) => {
        setConfirm(false);
        onDone(next);
        toast.success("اتصال حساب عملیاتی قطع شد", "نشست پنل در تلگرام بسته شد و از گاوصندوق پاک شد.");
      })
      .catch((err: unknown) => toast.error("اتصال قطع نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="flex flex-col gap-3">
      <AccountBox s={s} />
      <SessionFacts s={s} />
      {warning ? <Note tone={s.state === "error" ? "danger" : "warning"}>{warning}</Note> : null}
      {s.kill_switch ? (
        <Note tone="info" icon={<Info className="size-4" />}>
          کلید توقف روشن است؛ بررسی خودکار نشست (هر {toFaDigits(s.check_interval_min)} دقیقه) تا خاموش شدن آن متوقف است.
        </Note>
      ) : null}
      <Button variant="secondary" className="self-start" icon={<Unplug className="size-4" />} onClick={() => setConfirm(true)}>
        قطع اتصال
      </Button>
      <ConfirmDialog
        open={confirm}
        onOpenChange={(v) => {
          if (!busy) setConfirm(v);
        }}
        title="قطع اتصال حساب عملیاتی"
        description="نشست پنل در خود تلگرام بسته میشود (از فهرست دستگاه‌های اپ تلگرام هم برداشته میشود) و api_id، api_hash و نشست از گاوصندوق پنل پاک میشوند. برای وصل دوباره باید از اول وارد شوید."
        confirmLabel="قطع اتصال"
        tone="danger"
        icon={<Unplug className="size-6" />}
        busy={busy}
        onConfirm={logout}
      />
    </div>
  );
}

function Disconnected({ s, onDone }: { s: MtpAccount; onDone: (s: MtpAccount) => void }) {
  return (
    <div className="flex flex-col gap-4">
      <AccountBox s={s} />
      <Note tone="danger">
        <b>نشست بسته شد{s.disconnected_at ? ` (${formatRelative(s.disconnected_at)})` : ""}.</b> {disconnectReasonText(s.disconnect_reason)} برای
        وصل دوباره، از اول وارد شوید.
      </Note>
      <StartForm onDone={onDone} />
    </div>
  );
}

// --- the card ---------------------------------------------------------------------------------------

export function MtproxyAccountCard() {
  const qc = useQueryClient();
  const query = useMtproxyAccount();
  const s = query.data;
  const info = s ? mtpStateInfo(s.state) : null;

  const onDone = (next: MtpAccount) => {
    qc.setQueryData(keys.mtproxyAccount, next);
    void qc.invalidateQueries({ queryKey: ["events"] });
  };

  return (
    <Card className="lg:col-span-12">
      <CardHeader
        icon={<UserRoundCog className="size-5" />}
        title={<>حساب عملیاتی {BOT}</>}
        subtitle="حساب دوم تلگرام که پنل بعداً با آن پروکسی‌ها را در ‎@MTProxybot‎ ثبت میکند و اسپانسر ‎@ipoxxo‎ را میگذارد."
        action={info ? <Badge tone={info.tone} dot pulse={s ? isPending(s) : false}>{info.label}</Badge> : null}
      />
      <div className="grid gap-5 p-5 lg:grid-cols-2">
        <Rules />
        <div className="flex flex-col gap-3">
          {query.isPending ? (
            <SkeletonLines lines={5} />
          ) : query.isError ? (
            <LoadError
              error={query.error}
              compact
              onRetry={() => void query.refetch()}
              notYet={{
                title: "بخش حساب عملیاتی هنوز روی پنل فعال نشده",
                description: "ورود حساب دوم برای ‎@MTProxybot‎ با به‌روزرسانی بعدی پنل فعال میشود. کاری از شما لازم نیست.",
              }}
            />
          ) : s ? (
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={s.state}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={{ duration: 0.25 }}
                className="flex flex-col gap-3"
              >
                {s.notice === "login_expired" && s.state === "none" ? (
                  <Note tone="info" icon={<Info className="size-4" />}>
                    زمان ورود قبلی (۵ دقیقه) تمام شد. از اول شروع کنید تا کد تازه بیاید.
                  </Note>
                ) : null}
                {isPending(s) ? (
                  <StepForm key={`${s.state}-${s.login?.code_sent_at ?? ""}`} s={s} onDone={onDone} />
                ) : s.state === "disconnected" ? (
                  <Disconnected s={s} onDone={onDone} />
                ) : canStartLogin(s) ? (
                  <StartForm onDone={onDone} />
                ) : (
                  <Connected s={s} onDone={onDone} />
                )}
              </motion.div>
            </AnimatePresence>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
