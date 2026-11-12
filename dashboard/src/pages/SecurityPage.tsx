import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Laptop, LogOut, MonitorSmartphone, ShieldCheck, Smartphone, UserRound } from "lucide-react";
import { motion } from "motion/react";
import { useState, type SubmitEvent } from "react";
import { QRCodeSVG } from "qrcode.react";
import { useNavigate } from "react-router";

import { RecoveryCodesPanel } from "../components/RecoveryCodesPanel";
import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { ConfirmDialog, Modal } from "../components/ui/Dialogs";
import { CopyButton, ErrorState, SkeletonLines } from "../components/ui/Feedback";
import { Field, SecretInput, Segmented, TextInput } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage, isApiError } from "../lib/api";
import { dismissAllowed } from "../lib/dialogs";
import * as api from "../lib/endpoints";
import { formatJalali, formatRelative, groupSecret } from "../lib/format";
import { keys, useMe, useSessions } from "../lib/queries";
import { toast } from "../lib/toast";
import type { SessionInfo, TotpBegin } from "../lib/types";
import { isTotp, normalizeTotp, totpReenrollInput } from "../lib/validation";

/** A short, readable device name from the user agent (no fingerprinting, display only). */
function deviceName(ua: string | null): { name: string; mobile: boolean } {
  if (!ua) return { name: "دستگاه ناشناس", mobile: false };
  const mobile = /Mobile|Android|iPhone|iPad/i.test(ua);
  const os = /iPhone|iPad/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  const name = [browser, os].filter(Boolean).join(" روی ");
  return { name: name || "مرورگر", mobile };
}

function RegenerateDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);

  const close = (v: boolean) => {
    if (!dismissAllowed(busy, v)) return; // the new codes come back in this sheet only
    if (!v) {
      setCode("");
      setCodes(null);
      setError(null);
    }
    onOpenChange(v);
  };

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (!isTotp(code) || busy) return;
    setBusy(true);
    setError(null);
    api
      .regenerateRecoveryCodes(code)
      .then((res) => {
        setCodes(res.recovery_codes);
        toast.success("کدهای بازیابی تازه ساخته شد", "کدهای قبلی دیگر کار نمیکنند.");
      })
      .catch((err: unknown) => setError(isApiError(err) ? err.messageFa : errorMessage(err)))
      .finally(() => {
        setCode("");
        setBusy(false);
      });
  };

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="کدهای بازیابی تازه"
      description={codes ? "این کدها فقط همین یک بار نشان داده میشوند." : "با ساخت کدهای تازه، همه‌ی کدهای قبلی باطل میشوند."}
      icon={<KeyRound className="size-5" />}
    >
      {codes ? (
        <RecoveryCodesPanel
          codes={codes}
          title="کدهای بازیابی"
          intro="کدهای قبلی باطل شد. این ۱۰ کد را جای امنی نگه دارید؛ هر کدام یک بار کار میکند."
          continueLabel="تمام شد"
          onContinue={() => close(false)}
        />
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          <Field label="کد ۶ رقمی Authenticator" htmlFor="regen-totp" error={error} hint="برای تأیید، کد فعلی برنامه را وارد کنید.">
            <TextInput
              id="regen-totp"
              latin
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(normalizeTotp(e.target.value))}
              className="text-center text-xl tracking-[0.5em]"
              placeholder="123456"
              invalid={Boolean(error)}
            />
          </Field>
          <Button type="submit" size="lg" block loading={busy} disabled={!isTotp(code)} icon={<KeyRound className="size-5" />}>
            ساخت کدهای تازه
          </Button>
        </form>
      )}
    </Modal>
  );
}

/** A new Authenticator after the phone was lost (or replaced). The new secret, the password and the
 * codes live only in this sheet's state: never in a query cache, never logged (rule 3). */
function ReenrollDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const qc = useQueryClient();
  const [enroll, setEnroll] = useState<TotpBegin | null>(null);
  const [newCode, setNewCode] = useState("");
  const [password, setPassword] = useState("");
  const [proofKind, setProofKind] = useState<"recovery" | "totp">("recovery");
  const [proof, setProof] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const input = enroll ? totpReenrollInput(enroll.pending, newCode, password, { kind: proofKind, value: proof }) : null;

  const close = (v: boolean) => {
    if (!dismissAllowed(busy, v)) return;
    if (!v) {
      setEnroll(null);
      setNewCode("");
      setPassword("");
      setProof("");
      setCodes(null);
      setError(null);
    }
    onOpenChange(v);
  };

  const begin = () => {
    setBusy(true);
    setError(null);
    api
      .totpReenrollBegin()
      .then(setEnroll)
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (!input || busy) return;
    setBusy(true);
    setError(null);
    api
      .totpReenrollFinish(input)
      .then((res) => {
        setEnroll(null);
        setCodes(res.recovery_codes);
        void qc.invalidateQueries({ queryKey: keys.sessions });
        toast.success("Authenticator تازه ثبت شد", "جلسه‌های دیگر بسته شدند و کدهای بازیابی قبلی باطل شد.");
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => {
        setPassword("");
        setNewCode("");
        setProof("");
        setBusy(false);
      });
  };

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="Authenticator تازه"
      description={
        codes
          ? "کدهای بازیابی تازه فقط همین یک بار نشان داده میشوند."
          : "اگر گوشی گم شد یا عوض شد، برنامه‌ی Authenticator تازه را اینجا ثبت کنید. کد قبلی و کدهای بازیابی قبلی دیگر کار نمیکنند."
      }
      icon={<Smartphone className="size-5" />}
    >
      {codes ? (
        <RecoveryCodesPanel
          codes={codes}
          title="کدهای بازیابی"
          intro="Authenticator تازه ثبت شد و جلسه‌های دیگر بسته شدند. این ۱۰ کد را جای امنی نگه دارید؛ هر کدام یک بار کار میکند."
          continueLabel="تمام شد"
          onContinue={() => close(false)}
        />
      ) : !enroll ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm leading-7 text-fg-muted">
            پنل یک کلید تازه میسازد. آن را در برنامه‌ی Authenticator گوشی تازه ثبت میکنید و با رمز و یک کد بازیابی (یا کد برنامه‌ی قبلی، اگر هنوز دارید) تأیید میکنید. تا یک ساعت فرصت دارید.
          </p>
          {error ? (
            <p role="alert" className="rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-danger">
              {error}
            </p>
          ) : null}
          <Button size="lg" block loading={busy} onClick={begin} icon={<KeyRound className="size-5" />}>
            ساخت کلید تازه
          </Button>
        </div>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-stretch">
            <div className="rounded-3xl bg-white p-3 shadow-card ring-1 ring-line" data-ph-secret>
              <QRCodeSVG value={enroll.totp_uri} size={168} level="M" marginSize={1} bgColor="#ffffff" fgColor="#0b0d17" title="QR ثبت در Authenticator" />
            </div>
            <div className="flex w-full min-w-0 flex-1 flex-col gap-3">
              <a
                href={enroll.totp_uri}
                className="inline-flex h-11 items-center justify-center gap-2 rounded-2xl border border-line bg-surface-2 px-4 text-sm font-semibold text-fg"
              >
                <Smartphone className="size-4" />
                افزودن به Authenticator همین گوشی
              </a>
              <div className="rounded-2xl border border-line bg-surface-2 p-3">
                <div className="mb-1 text-xs font-semibold text-fg-muted">کلید دستی</div>
                <code className="ltr block font-mono text-[0.95rem] leading-7 font-bold break-all text-fg" data-ph-secret>
                  {groupSecret(enroll.totp_secret)}
                </code>
                <CopyButton text={enroll.totp_secret} label="کپی کلید" className="mt-2" />
              </div>
            </div>
          </div>
          <Field label="کد ۶ رقمی برنامه‌ی تازه" htmlFor="reenroll-new" hint="کدی که برنامه‌ی Authenticator تازه همین حالا نشان میدهد.">
            <TextInput
              id="reenroll-new"
              latin
              inputMode="numeric"
              autoComplete="one-time-code"
              value={newCode}
              onChange={(e) => setNewCode(normalizeTotp(e.target.value))}
              className="text-center text-xl tracking-[0.5em]"
              placeholder="123456"
            />
          </Field>
          <Field label="رمز ورود" htmlFor="reenroll-password">
            <SecretInput id="reenroll-password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <div className="flex flex-col gap-2">
            <Segmented<"recovery" | "totp">
              value={proofKind}
              onChange={(v) => {
                setProofKind(v);
                setProof("");
              }}
              label="تأیید با"
              options={[
                { value: "recovery", label: "کد بازیابی" },
                { value: "totp", label: "کد برنامه‌ی قبلی" },
              ]}
              className="self-start"
            />
            {proofKind === "recovery" ? (
              <Field label="یکی از کدهای بازیابی" htmlFor="reenroll-proof" hint="همان کدهایی که هنگام راه‌اندازی یا ساخت کدهای تازه گرفتید.">
                <TextInput id="reenroll-proof" latin autoComplete="off" value={proof} onChange={(e) => setProof(e.target.value)} />
              </Field>
            ) : (
              <Field label="کد ۶ رقمی برنامه‌ی قبلی" htmlFor="reenroll-proof">
                <TextInput
                  id="reenroll-proof"
                  latin
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={proof}
                  onChange={(e) => setProof(normalizeTotp(e.target.value))}
                  className="text-center text-xl tracking-[0.5em]"
                  placeholder="123456"
                />
              </Field>
            )}
          </div>
          {error ? (
            <p role="alert" className="rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-danger">
              {error}
            </p>
          ) : null}
          <Button type="submit" size="lg" block loading={busy} disabled={input === null} icon={<ShieldCheck className="size-5" />}>
            ثبت Authenticator تازه
          </Button>
        </form>
      )}
    </Modal>
  );
}

function SessionRow({ s, index, onRevoke }: { s: SessionInfo; index: number; onRevoke: () => void }) {
  const d = deviceName(s.user_agent);
  const Icon = d.mobile ? Smartphone : Laptop;
  return (
    <motion.li
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.05 * index }}
      className="flex items-center gap-3 px-2 py-3"
    >
      <span className={s.current ? "grid size-11 place-items-center rounded-2xl bg-brand-soft text-brand" : "grid size-11 place-items-center rounded-2xl bg-surface-3 text-fg-muted"}>
        <Icon className="size-5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-bold text-fg">{d.name}</span>
          {s.current ? (
            <Badge tone="brand" dot pulse>
              همین دستگاه
            </Badge>
          ) : null}
        </div>
        <div className="text-xs leading-6 text-fg-muted">
          آخرین فعالیت {formatRelative(s.last_seen_at)} · ورود {formatJalali(s.created_at, "short")}
        </div>
      </div>
      <Button variant={s.current ? "secondary" : "ghost"} size="sm" className={s.current ? "" : "text-danger"} icon={<LogOut className="size-4 rtl:-scale-x-100" />} onClick={onRevoke}>
        {s.current ? "خروج" : "بیرون کردن"}
      </Button>
    </motion.li>
  );
}

export default function SecurityPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const me = useMe();
  const sessions = useSessions();
  const [regen, setRegen] = useState(false);
  const [reenroll, setReenroll] = useState(false);
  const [revoke, setRevoke] = useState<SessionInfo | null>(null);
  const [busy, setBusy] = useState(false);

  const doRevoke = () => {
    if (!revoke) return;
    setBusy(true);
    const wasCurrent = revoke.current;
    api
      .revokeSession(revoke.id)
      .then(() => {
        setRevoke(null);
        if (wasCurrent) {
          qc.clear();
          void navigate("/login", { replace: true });
          toast.info("از پنل خارج شدید");
          return;
        }
        qc.setQueryData<SessionInfo[]>(keys.sessions, (old) => old?.filter((x) => x.id !== revoke.id));
        void qc.invalidateQueries({ queryKey: keys.sessions });
        toast.success("جلسه بسته شد", "آن دستگاه دیگر به پنل دسترسی ندارد.");
      })
      .catch((e: unknown) => toast.error("جلسه بسته نشد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const list = [...(sessions.data ?? [])].sort((a, b) => Number(b.current) - Number(a.current) || b.last_seen_at.localeCompare(a.last_seen_at));

  return (
    <>
      <PageHeader title="امنیت" subtitle="حساب مالک، جلسه‌های فعال و کدهای بازیابی ورود." />
      <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
        <div className="flex flex-col gap-4 sm:gap-5 lg:col-span-5">
          <Card>
            <CardHeader icon={<UserRound className="size-5" />} title="حساب مالک" />
            <div className="p-5">
              {me.isPending ? (
                <SkeletonLines lines={3} />
              ) : me.isLoadingError ? (
                <ErrorState error={me.error} compact onRetry={() => void me.refetch()} />
              ) : (
                <dl className="grid grid-cols-[7.5rem_1fr] gap-y-3 text-sm">
                  <dt className="text-fg-muted">نام کاربری</dt>
                  <dd className="ltr text-right font-bold text-fg">{me.data.username}</dd>
                  <dt className="text-fg-muted">ساخته‌شده</dt>
                  <dd className="text-fg">{formatJalali(me.data.created_at, "date")}</dd>
                  <dt className="text-fg-muted">آخرین ورود</dt>
                  <dd className="text-fg">{me.data.last_login_at ? `${formatJalali(me.data.last_login_at, "datetime")} (${formatRelative(me.data.last_login_at)})` : "—"}</dd>
                  <dt className="text-fg-muted">ورود دومرحله‌ای</dt>
                  <dd>
                    <Badge tone="success" dot>
                      فعال (Authenticator)
                    </Badge>
                  </dd>
                </dl>
              )}
            </div>
          </Card>
          <Card>
            <CardHeader icon={<KeyRound className="size-5" />} title="کدهای بازیابی" subtitle="اگر گوشی گم شد، با این کدها وارد میشوید." />
            <div className="flex flex-col gap-3 p-5">
              <p className="text-sm leading-7 text-fg-muted">
                ساخت کدهای تازه همه‌ی کدهای قبلی را باطل میکند. اگر فکر میکنید کسی کدها را دیده، کدهای تازه بسازید.
              </p>
              <Button variant="secondary" icon={<ShieldCheck className="size-4" />} onClick={() => setRegen(true)} className="self-start">
                ساخت کدهای تازه
              </Button>
              <p className="border-t border-line pt-3 text-sm leading-7 text-fg-muted">
                گوشی گم شد یا عوض شد؟ با رمز و یک کد بازیابی، برنامه‌ی Authenticator تازه را ثبت کنید؛ پس از آن کدهای بازیابی تازه میگیرید.
              </p>
              <Button variant="secondary" icon={<Smartphone className="size-4" />} onClick={() => setReenroll(true)} className="self-start">
                ثبت Authenticator تازه
              </Button>
            </div>
          </Card>
        </div>

        <Card className="lg:col-span-7">
          <CardHeader
            icon={<MonitorSmartphone className="size-5" />}
            title="جلسه‌های فعال"
            subtitle="هر دستگاهی که الان به پنل وارد است. دستگاه ناآشنا را بیرون کنید."
          />
          <div className="p-3 sm:p-4">
            {sessions.isPending ? (
              <SkeletonLines lines={4} className="p-2" />
            ) : sessions.isLoadingError ? (
              <ErrorState error={sessions.error} compact onRetry={() => void sessions.refetch()} />
            ) : (
              <ul className="flex flex-col divide-y divide-line">
                {list.map((s, i) => (
                  <SessionRow key={s.id} s={s} index={i} onRevoke={() => setRevoke(s)} />
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>

      <RegenerateDialog open={regen} onOpenChange={setRegen} />
      <ReenrollDialog open={reenroll} onOpenChange={setReenroll} />
      <ConfirmDialog
        open={revoke !== null}
        onOpenChange={(v) => {
          if (!v && !busy) setRevoke(null);
        }}
        title={revoke?.current ? "خروج از همین دستگاه؟" : "بیرون کردن این جلسه؟"}
        description={
          revoke?.current
            ? "جلسه‌ی همین مرورگر بسته میشود و دوباره باید وارد شوید."
            : "آن دستگاه فوراً از پنل بیرون میشود و برای ورود دوباره رمز و کد لازم دارد."
        }
        confirmLabel={revoke?.current ? "خروج" : "بیرون کن"}
        tone="danger"
        icon={<LogOut className="size-6 rtl:-scale-x-100" />}
        busy={busy}
        onConfirm={doRevoke}
      />
    </>
  );
}
