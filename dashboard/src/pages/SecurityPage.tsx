import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Laptop, LogOut, MonitorSmartphone, ShieldCheck, Smartphone, UserRound } from "lucide-react";
import { motion } from "motion/react";
import { useState, type SubmitEvent } from "react";
import { useNavigate } from "react-router";

import { RecoveryCodesPanel } from "../components/RecoveryCodesPanel";
import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { ConfirmDialog, Modal } from "../components/ui/Dialogs";
import { ErrorState, SkeletonLines } from "../components/ui/Feedback";
import { Field, TextInput } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage, isApiError } from "../lib/api";
import * as api from "../lib/endpoints";
import { formatJalali, formatRelative } from "../lib/format";
import { keys, useMe, useSessions } from "../lib/queries";
import { toast } from "../lib/toast";
import type { SessionInfo } from "../lib/types";
import { isTotp, normalizeTotp } from "../lib/validation";

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
      <Button variant={s.current ? "secondary" : "ghost"} size="sm" className={s.current ? "" : "text-danger"} icon={<LogOut className="size-4" />} onClick={onRevoke}>
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
              ) : me.isError ? (
                <ErrorState error={me.error} compact onRetry={() => void me.refetch()} />
              ) : (
                <dl className="grid grid-cols-[7.5rem_1fr] gap-y-3 text-sm">
                  <dt className="text-fg-muted">نام کاربری</dt>
                  <dd className="ltr text-start font-bold text-fg">{me.data.username}</dd>
                  <dt className="text-fg-muted">ساخته‌شده</dt>
                  <dd className="text-fg">{formatJalali(me.data.created_at, "date")}</dd>
                  <dt className="text-fg-muted">ورود قبلی</dt>
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
            ) : sessions.isError ? (
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
        icon={<LogOut className="size-6" />}
        busy={busy}
        onConfirm={doRevoke}
      />
    </>
  );
}
