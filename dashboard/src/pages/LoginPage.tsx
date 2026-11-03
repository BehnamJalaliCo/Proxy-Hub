import { useQueryClient } from "@tanstack/react-query";
import { Hourglass, KeyRound, LifeBuoy, LogIn, Smartphone, Sparkles } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState, type SubmitEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { AuthLayout } from "../components/layout/AuthLayout";
import { Button } from "../components/ui/Button";
import { Field, SecretInput, TextInput } from "../components/ui/Form";
import { isApiError, safeNext } from "../lib/api";
import * as api from "../lib/endpoints";
import { formatDurationSeconds } from "../lib/format";
import { keys, useSetupStatus } from "../lib/queries";
import { toast } from "../lib/toast";
import { isTotp, normalizeDigits, normalizeTotp } from "../lib/validation";

type Mode = "totp" | "recovery";

function useCountdown(until: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (until === null) return;
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(t);
  }, [until]);
  return until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
}

export default function LoginPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const setup = useSetupStatus();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState("");
  const [mode, setMode] = useState<Mode>("totp");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState<number | null>(null);
  const remaining = useCountdown(lockedUntil);
  const locked = remaining > 0;

  const canSubmit =
    username.trim() !== "" && password !== "" && (mode === "totp" ? isTotp(code) : recovery.trim().length >= 6) && !locked;

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (!canSubmit || busy) return;
    setBusy(true);
    setError(null);
    const base = { username: username.trim(), password };
    const input =
      mode === "totp" ? { ...base, totp_code: code } : { ...base, recovery_code: normalizeDigits(recovery.trim()) };
    api
      .login(input)
      .then((res) => {
        setPassword("");
        setCode("");
        setRecovery("");
        qc.removeQueries({ queryKey: keys.me });
        toast.success("خوش آمدید", `با نام ${res.username} وارد شدید.`);
        void navigate(safeNext(params.get("next")), { replace: true });
      })
      .catch((err: unknown) => {
        setCode("");
        setRecovery("");
        if (isApiError(err) && err.code === "locked") {
          const s = err.num("retry_after_s") ?? 60;
          setLockedUntil(Date.now() + s * 1000);
          setError(err.messageFa);
        } else if (isApiError(err)) {
          setError(err.messageFa);
        } else {
          setError("اتصال به پنل برقرار نشد.");
        }
      })
      .finally(() => setBusy(false));
  };

  return (
    <AuthLayout>
      <div className="rounded-[2rem] border border-line bg-surface/90 p-6 shadow-pop backdrop-blur-xl sm:p-8">
        <div className="mb-6 flex items-center gap-3">
          <span className="grid size-12 place-items-center rounded-2xl bg-[linear-gradient(135deg,var(--ph-brand),var(--ph-brand-2))] text-white shadow-[0_10px_30px_-10px_var(--ph-brand)]">
            <LogIn className="size-6" />
          </span>
          <div>
            <h1 className="text-xl leading-9 font-extrabold text-fg">ورود به پنل</h1>
            <p className="text-sm text-fg-muted">نام کاربری، رمز و کد برنامه‌ی Authenticator</p>
          </div>
        </div>

        <AnimatePresence>
          {setup.data?.setup_required ? (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div className="mb-5 flex gap-3 rounded-2xl border border-brand/25 bg-brand-soft p-4 text-sm leading-7 text-fg">
                <Sparkles className="mt-1 size-5 shrink-0 text-brand" />
                <p>
                  پنل هنوز راه‌اندازی نشده است. <b>لینک یک‌بارمصرف راه‌اندازی</b> را از Claude روی سرور پنل بگیرید و همان
                  لینک را کامل باز کنید.
                </p>
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>

        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          <Field label="نام کاربری" htmlFor="login-username">
            <TextInput
              id="login-username"
              name="username"
              latin
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="owner"
              required
            />
          </Field>
          <Field label="رمز" htmlFor="login-password">
            <SecretInput
              id="login-password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </Field>

          <AnimatePresence mode="wait" initial={false}>
            {mode === "totp" ? (
              <motion.div key="totp" initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -16 }}>
                <Field
                  label="کد ۶ رقمی Authenticator"
                  htmlFor="login-totp"
                  hint="کد تازه‌ی برنامه را وارد کنید؛ هر ۳۰ ثانیه عوض میشود."
                >
                  <TextInput
                    id="login-totp"
                    name="totp"
                    latin
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={8}
                    value={code}
                    onChange={(e) => setCode(normalizeTotp(e.target.value))}
                    placeholder="123456"
                    className="text-center text-xl tracking-[0.5em]"
                  />
                </Field>
              </motion.div>
            ) : (
              <motion.div key="rec" initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -16 }}>
                <Field label="کد بازیابی" htmlFor="login-recovery" hint="هر کد بازیابی فقط یک بار کار میکند.">
                  <TextInput
                    id="login-recovery"
                    name="recovery"
                    latin
                    autoComplete="off"
                    spellCheck={false}
                    value={recovery}
                    onChange={(e) => setRecovery(e.target.value)}
                    placeholder="xxxx-xxxx"
                  />
                </Field>
              </motion.div>
            )}
          </AnimatePresence>

          <button
            type="button"
            onClick={() => {
              setMode((m) => (m === "totp" ? "recovery" : "totp"));
              setError(null);
            }}
            className="inline-flex items-center gap-1.5 self-start rounded-lg px-1 text-sm font-semibold text-brand hover:underline"
          >
            {mode === "totp" ? <LifeBuoy className="size-4" /> : <Smartphone className="size-4" />}
            {mode === "totp" ? "به گوشی دسترسی ندارم؛ ورود با کد بازیابی" : "ورود با کد برنامه‌ی Authenticator"}
          </button>

          <AnimatePresence>
            {error ? (
              <motion.div
                role="alert"
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                className="flex gap-3 rounded-2xl border border-danger/25 bg-danger-soft p-3.5 text-sm leading-7 text-fg"
              >
                {locked ? <Hourglass className="mt-1 size-5 shrink-0 text-danger" /> : <KeyRound className="mt-1 size-5 shrink-0 text-danger" />}
                <div>
                  <p>{error}</p>
                  {locked ? (
                    <p className="mt-1 font-bold text-danger">زمان باقی‌مانده: {formatDurationSeconds(remaining)}</p>
                  ) : null}
                </div>
              </motion.div>
            ) : null}
          </AnimatePresence>

          <Button type="submit" size="lg" block loading={busy} disabled={!canSubmit} icon={<LogIn className="size-5" />}>
            ورود
          </Button>
        </form>
      </div>
      {setup.data?.setup_required ? (
        <p className="mt-4 text-center text-sm text-fg-muted">
          لینک راه‌اندازی دارید؟{" "}
          <Link to="/setup" className="font-bold text-brand hover:underline">
            صفحه‌ی راه‌اندازی
          </Link>
        </p>
      ) : null}
    </AuthLayout>
  );
}
