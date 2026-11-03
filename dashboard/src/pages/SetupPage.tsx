import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  BadgeCheck,
  CircleAlert,
  KeyRound,
  Link2Off,
  LogIn,
  QrCode,
  Rocket,
  ShieldCheck,
  Smartphone,
  UserRound,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { QRCodeSVG } from "qrcode.react";
import { useEffect, useState, type SubmitEvent, type ReactNode } from "react";
import { useNavigate } from "react-router";

import { AuthLayout } from "../components/layout/AuthLayout";
import { RecoveryCodesPanel } from "../components/RecoveryCodesPanel";
import { Button, LinkButton } from "../components/ui/Button";
import { CopyButton, ErrorState, SkeletonLines } from "../components/ui/Feedback";
import { Field, SecretInput, TextInput } from "../components/ui/Form";
import { cn } from "../lib/browser";
import { isApiError } from "../lib/api";
import * as api from "../lib/endpoints";
import { groupSecret, toFaDigits } from "../lib/format";
import { keys, useSetupStatus } from "../lib/queries";
import type { SetupBegin } from "../lib/types";
import { isTotp, normalizeTotp, passwordError, passwordScore, usernameError } from "../lib/validation";

/** The setup link is /setup#<token>. The fragment never reaches a server or a log. */
function readTokenFromHash(): string {
  const raw = window.location.hash.replace(/^#/, "");
  const value = raw.startsWith("token=") ? raw.slice(6) : raw;
  try {
    return decodeURIComponent(value).trim();
  } catch {
    return value.trim();
  }
}

type Step = "intro" | "app" | "account" | "codes";
const STEPS: { id: Step; label: string }[] = [
  { id: "intro", label: "شروع" },
  { id: "app", label: "برنامه‌ی کد" },
  { id: "account", label: "حساب" },
  { id: "codes", label: "کدها" },
];

function Stepper({ step }: { step: Step }) {
  const index = STEPS.findIndex((s) => s.id === step);
  return (
    <ol className="mb-6 grid grid-cols-4 gap-2" aria-label="مراحل راه‌اندازی">
      {STEPS.map((s, i) => {
        const done = i < index;
        const current = i === index;
        return (
          <li key={s.id} className="flex flex-col gap-2" aria-current={current ? "step" : undefined}>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface-3">
              <motion.div
                className="h-full rounded-full bg-[linear-gradient(90deg,var(--ph-brand-2),var(--ph-brand))]"
                initial={false}
                animate={{ width: done || current ? "100%" : "0%" }}
                transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
                style={{ marginInlineStart: 0 }}
              />
            </div>
            <span className={cn("truncate text-[0.72rem] font-semibold", current ? "text-fg" : done ? "text-brand" : "text-fg-faint")}>
              {toFaDigits(i + 1)}. {s.label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

const slide = {
  initial: { opacity: 0, x: -24 },
  animate: { opacity: 1, x: 0, transition: { duration: 0.32, ease: [0.16, 1, 0.3, 1] as const } },
  exit: { opacity: 0, x: 24, transition: { duration: 0.15 } },
};

const STRENGTH = [
  { label: "", cls: "bg-surface-3" },
  { label: "کوتاه", cls: "bg-danger" },
  { label: "قابل قبول", cls: "bg-warning" },
  { label: "خوب", cls: "bg-success" },
  { label: "عالی", cls: "bg-success" },
];

export default function SetupPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const status = useSetupStatus();
  const [token] = useState(readTokenFromHash);
  const [step, setStep] = useState<Step>("intro");
  const [enrollment, setEnrollment] = useState<SetupBegin | null>(null);
  const [codes, setCodes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [totp, setTotp] = useState("");
  const [touched, setTouched] = useState(false);
  const [serverErr, setServerErr] = useState<{ field: "username" | "password" | "totp" | null; text: string } | null>(null);

  // Drop the token from the address bar and the history entry; it stays in memory only.
  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
  }, []);

  const begin = () => {
    setBusy(true);
    setFatal(null);
    api
      .setupBegin(token)
      .then((res) => {
        setEnrollment(res);
        setStep("app");
      })
      .catch((err: unknown) => {
        setFatal(isApiError(err) ? err.messageFa : "اتصال به پنل برقرار نشد.");
      })
      .finally(() => setBusy(false));
  };

  const uErr = usernameError(username);
  const pErr = passwordError(password, username);
  const p2Err = password2 !== password ? "دو رمز یکی نیستند." : null;
  const formOk = !uErr && !pErr && !p2Err && isTotp(totp);

  const finish = (e: SubmitEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!formOk || !enrollment || busy) return;
    setBusy(true);
    setServerErr(null);
    api
      .setupFinish({ token, enrollment_id: enrollment.enrollment_id, username, password, totp_code: totp })
      .then((res) => {
        setPassword("");
        setPassword2("");
        setTotp("");
        setEnrollment(null);
        setCodes(res.recovery_codes);
        setStep("codes");
        qc.setQueryData(keys.setupStatus, { setup_required: false });
      })
      .catch((err: unknown) => {
        setTotp("");
        if (!isApiError(err)) {
          setServerErr({ field: null, text: "اتصال به پنل برقرار نشد." });
          return;
        }
        if (err.code === "setup_token_invalid") {
          setFatal(err.messageFa);
          return;
        }
        const field = err.code === "invalid_username" ? "username" : err.code === "weak_password" ? "password" : err.code === "totp_invalid" ? "totp" : null;
        setServerErr({ field, text: err.messageFa });
      })
      .finally(() => setBusy(false));
  };

  const score = passwordScore(password);

  let body: ReactNode;
  if (status.isPending) {
    body = <SkeletonLines lines={4} />;
  } else if (status.isError && step === "intro") {
    body = <ErrorState error={status.error} onRetry={() => void status.refetch()} compact />;
  } else if (status.data?.setup_required === false && step !== "codes") {
    body = (
      <div className="flex flex-col items-center gap-4 py-4 text-center">
        <span className="grid size-16 place-items-center rounded-3xl bg-success-soft text-success">
          <BadgeCheck className="size-8" />
        </span>
        <h2 className="text-lg font-bold">پنل پیش‌تر راه‌اندازی شده است</h2>
        <p className="text-sm leading-7 text-fg-muted">حساب مالک ساخته شده و لینک راه‌اندازی دیگر کار نمیکند. از صفحه‌ی ورود وارد شوید.</p>
        <LinkButton to="/login" block size="lg" icon={<LogIn className="size-5 rtl:-scale-x-100" />}>
          رفتن به صفحه‌ی ورود
        </LinkButton>
      </div>
    );
  } else if (fatal || (!token && step === "intro")) {
    body = (
      <div className="flex flex-col items-center gap-4 py-4 text-center">
        <span className="grid size-16 place-items-center rounded-3xl bg-danger-soft text-danger">
          <Link2Off className="size-8" />
        </span>
        <h2 className="text-lg font-bold">{fatal ? "لینک راه‌اندازی کار نکرد" : "لینک راه‌اندازی لازم است"}</h2>
        <p className="text-sm leading-7 text-fg-muted">
          {fatal ??
            "این صفحه فقط با لینک یک‌بارمصرف راه‌اندازی باز میشود. لینک را از Claude روی سرور پنل بگیرید و همان لینک را کامل باز کنید."}
        </p>
        {fatal ? (
          <p className="text-sm leading-7 text-fg-muted">لینک تازه بگیرید؛ هر لینک ۶۰ دقیقه و فقط یک بار کار میکند.</p>
        ) : null}
      </div>
    );
  } else {
    body = (
      <>
        <Stepper step={step} />
        <AnimatePresence mode="wait" initial={false}>
          {step === "intro" ? (
            <motion.div key="intro" {...slide} className="flex flex-col gap-5">
              <div className="flex items-center gap-3">
                <span className="grid size-12 place-items-center rounded-2xl bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))] text-white">
                  <Rocket className="size-6" />
                </span>
                <div>
                  <h1 className="text-xl leading-9 font-extrabold">راه‌اندازی پنل</h1>
                  <p className="text-sm text-fg-muted">ساخت حساب مالک با ورود دومرحله‌ای</p>
                </div>
              </div>
              <ul className="flex flex-col gap-3 text-sm leading-7 text-fg">
                {[
                  { icon: Smartphone, t: "برنامه‌ی Authenticator گوشی (مثل Google Authenticator، Microsoft Authenticator یا Aegis) را آماده کنید." },
                  { icon: UserRound, t: "نام کاربری و یک رمز قوی (دست‌کم ۱۲ نویسه) انتخاب میکنید." },
                  { icon: KeyRound, t: "۱۰ کد بازیابی یک‌بارمصرف میگیرید؛ آن‌ها را جای امنی نگه دارید." },
                ].map(({ icon: Icon, t }) => (
                  <li key={t} className="flex gap-3 rounded-2xl border border-line bg-surface-2/70 p-3">
                    <Icon className="mt-1 size-5 shrink-0 text-brand" />
                    <span>{t}</span>
                  </li>
                ))}
              </ul>
              <Button size="lg" block loading={busy} onClick={begin} icon={<ShieldCheck className="size-5" />}>
                شروع راه‌اندازی
              </Button>
            </motion.div>
          ) : step === "app" && enrollment ? (
            <motion.div key="app" {...slide} className="flex flex-col gap-5">
              <div>
                <h1 className="text-xl leading-9 font-extrabold">ثبت در برنامه‌ی Authenticator</h1>
                <p className="text-sm leading-7 text-fg-muted">
                  روی گوشی، دکمه‌ی زیر را بزنید یا کلید را دستی وارد کنید. روی کامپیوتر، QR را با برنامه اسکن کنید.
                </p>
              </div>
              <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-stretch">
                <motion.div
                  initial={{ scale: 0.9, opacity: 0, rotate: -2 }}
                  animate={{ scale: 1, opacity: 1, rotate: 0 }}
                  transition={{ type: "spring", stiffness: 220, damping: 18 }}
                  className="rounded-3xl bg-white p-3 shadow-card ring-1 ring-line"
                  data-ph-secret
                >
                  <QRCodeSVG
                    value={enrollment.totp_uri}
                    size={184}
                    level="M"
                    marginSize={1}
                    bgColor="#ffffff"
                    fgColor="#0b0d17"
                    title="QR ثبت در Authenticator"
                  />
                </motion.div>
                <div className="flex w-full min-w-0 flex-1 flex-col gap-3">
                  <a
                    href={enrollment.totp_uri}
                    className="inline-flex h-12 items-center justify-center gap-2 rounded-2xl bg-[linear-gradient(110deg,var(--ph-brand),var(--ph-brand-2))] px-4 font-semibold text-white shadow-[0_8px_24px_-10px_var(--ph-brand)]"
                  >
                    <Smartphone className="size-5" />
                    افزودن به Authenticator همین گوشی
                  </a>
                  <div className="rounded-2xl border border-line bg-surface-2 p-3">
                    <div className="mb-1 flex items-center gap-2 text-xs font-semibold text-fg-muted">
                      <QrCode className="size-4" /> کلید دستی
                    </div>
                    <code className="ltr block font-mono text-[0.95rem] leading-7 font-bold break-all text-fg" data-ph-secret>
                      {groupSecret(enrollment.totp_secret)}
                    </code>
                    <CopyButton text={enrollment.totp_secret} label="کپی کلید" className="mt-2" />
                  </div>
                </div>
              </div>
              <p className="flex gap-2 rounded-2xl bg-warning-soft p-3 text-[0.85rem] leading-7 text-fg">
                <CircleAlert className="mt-1 size-4 shrink-0 text-warning" />
                این کلید فقط همین یک بار نشان داده میشود. اگر صفحه را ببندید، لینک راه‌اندازی تازه لازم است.
              </p>
              <Button size="lg" block onClick={() => setStep("account")} icon={<ArrowLeft className="size-5" />}>
                ثبت کردم؛ ادامه
              </Button>
            </motion.div>
          ) : step === "account" ? (
            <motion.form key="account" {...slide} onSubmit={finish} noValidate className="flex flex-col gap-4">
              <div>
                <h1 className="text-xl leading-9 font-extrabold">حساب مالک</h1>
                <p className="text-sm leading-7 text-fg-muted">این‌ها همان چیزهایی است که هر بار برای ورود لازم دارید.</p>
              </div>
              <Field
                label="نام کاربری"
                htmlFor="setup-username"
                hint="۳ تا ۳۲ نویسه: حروف کوچک انگلیسی، رقم، نقطه، خط تیره یا زیرخط."
                error={serverErr?.field === "username" ? serverErr.text : touched || username ? uErr : null}
              >
                <TextInput
                  id="setup-username"
                  latin
                  autoComplete="username"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={username}
                  onChange={(e) => setUsername(e.target.value.toLowerCase())}
                  invalid={Boolean((touched || username) && uErr) || serverErr?.field === "username"}
                  placeholder="owner"
                />
              </Field>
              <Field
                label="رمز"
                htmlFor="setup-password"
                hint="دست‌کم ۱۲ نویسه؛ رمز رایج نباشد و نام کاربری در آن نباشد."
                error={serverErr?.field === "password" ? serverErr.text : touched || password ? pErr : null}
                trailing={
                  password ? (
                    <span className="flex items-center gap-2 text-xs text-fg-muted">
                      <span className="flex gap-1">
                        {[1, 2, 3, 4].map((i) => (
                          <span key={i} className={cn("h-1.5 w-5 rounded-full transition-colors", i <= score ? STRENGTH[score]?.cls : "bg-surface-3")} />
                        ))}
                      </span>
                      {STRENGTH[score]?.label}
                    </span>
                  ) : null
                }
              >
                <SecretInput
                  id="setup-password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  invalid={Boolean((touched || password) && pErr) || serverErr?.field === "password"}
                />
              </Field>
              <Field label="تکرار رمز" htmlFor="setup-password2" error={(touched || password2) && p2Err ? p2Err : null}>
                <SecretInput
                  id="setup-password2"
                  autoComplete="new-password"
                  value={password2}
                  onChange={(e) => setPassword2(e.target.value)}
                  invalid={Boolean((touched || password2) && p2Err)}
                />
              </Field>
              <Field
                label="کد ۶ رقمی Authenticator"
                htmlFor="setup-totp"
                hint="کد تازه‌ای که برنامه برای این پنل نشان میدهد."
                error={serverErr?.field === "totp" ? serverErr.text : touched && !isTotp(totp) ? "کد ۶ رقمی را وارد کنید." : null}
              >
                <TextInput
                  id="setup-totp"
                  latin
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={totp}
                  onChange={(e) => setTotp(normalizeTotp(e.target.value))}
                  className="text-center text-xl tracking-[0.5em]"
                  placeholder="123456"
                  invalid={serverErr?.field === "totp"}
                />
              </Field>
              {serverErr && serverErr.field === null ? (
                <p role="alert" className="rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-danger">
                  {serverErr.text}
                </p>
              ) : null}
              <div className="flex flex-col-reverse gap-2 sm:flex-row">
                <Button variant="secondary" onClick={() => setStep("app")} disabled={busy}>
                  بازگشت به QR
                </Button>
                <Button type="submit" size="lg" className="sm:flex-1" loading={busy} icon={<ShieldCheck className="size-5" />}>
                  ساخت حساب مالک
                </Button>
              </div>
            </motion.form>
          ) : step === "codes" ? (
            <motion.div key="codes" {...slide}>
              <RecoveryCodesPanel
                codes={codes}
                title="کدهای بازیابی"
                intro="حساب مالک ساخته شد و وارد پنل شدید. اگر گوشی در دسترس نبود، با هر کدام از این کدها یک بار میتوانید وارد شوید. این کدها فقط همین یک بار نشان داده میشوند."
                continueLabel="ورود به پنل"
                onContinue={() => {
                  setCodes([]);
                  qc.removeQueries({ queryKey: keys.me });
                  void navigate("/", { replace: true });
                }}
              />
            </motion.div>
          ) : null}
        </AnimatePresence>
      </>
    );
  }

  return (
    <AuthLayout wide={step === "app" || step === "codes"}>
      <div className="rounded-[2rem] border border-line bg-surface/90 p-6 shadow-pop backdrop-blur-xl sm:p-8">{body}</div>
    </AuthLayout>
  );
}
