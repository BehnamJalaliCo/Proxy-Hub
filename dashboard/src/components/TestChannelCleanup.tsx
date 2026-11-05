import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useState, type SubmitEvent } from "react";

import { errorMessage } from "../lib/api";
import {
  cleanSummary,
  cleanTestChannel,
  confirmMatches,
  CONFIRM_TEXT,
  getTestChannelCleanup,
  type TestChannelPreview,
  type TestChannelResult,
} from "../lib/cleanup";
import { formatNumber, toFaDigits } from "../lib/format";
import { keys } from "../lib/queries";
import { toast } from "../lib/toast";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Dialogs";
import { Skeleton } from "./ui/Feedback";
import { Field, TextInput } from "./ui/Form";

/**
 * «پاک کردن کانال آزمایشی» (decision D44): every message of the TEST channel goes, also the second
 * account's posts. Never the main channel: the server refuses it in every case. The dialog shows
 * what the server found (the bot's rights are read live from Telegram) and needs «پاک کن» typed.
 */
export function TestChannelCleanupButton({ disabled = false }: { disabled?: boolean }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<TestChannelPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TestChannelResult | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    getTestChannelCleanup()
      .then(setPreview)
      .catch((e: unknown) => {
        setPreview(null);
        setError(errorMessage(e));
      })
      .finally(() => setLoading(false));
  };

  const openDialog = () => {
    setOpen(true);
    setTyped("");
    setResult(null);
    load();
  };

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (!confirmMatches(typed) || busy) return;
    setBusy(true);
    setError(null);
    cleanTestChannel(typed)
      .then((r) => {
        setResult(r);
        void qc.invalidateQueries({ queryKey: keys.telegram });
        void qc.invalidateQueries({ queryKey: ["posts"] });
        void qc.invalidateQueries({ queryKey: ["events"] });
        if (r.finished) toast.success("کانال آزمایشی پاک شد", cleanSummary(r));
        else toast.warning("کانال آزمایشی کامل پاک نشد", r.stop_fa ?? cleanSummary(r));
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };

  const ready = preview !== null && preview.blocked === null;

  return (
    <>
      <Button variant="secondary" size="sm" icon={<Trash2 className="size-4" />} disabled={disabled} onClick={openDialog}>
        پاک کردن کانال آزمایشی
      </Button>
      <Modal
        open={open}
        onOpenChange={(v) => {
          if (!busy) setOpen(v);
        }}
        title="پاک کردن کانال آزمایشی"
        description="همه‌ی پیام‌های کانال آزمایشی پاک میشوند، پست‌های ربات و حساب دوم هر دو. به کانال اصلی هرگز دست زده نمیشود."
        icon={<Trash2 className="size-5" />}
      >
        {result ? (
          <Done result={result} onClose={() => setOpen(false)} />
        ) : (
          <form onSubmit={submit} noValidate className="flex flex-col gap-4">
            {loading ? (
              <div className="flex flex-col gap-2" aria-busy>
                <Skeleton className="h-6 w-2/3" />
                <Skeleton className="h-16 w-full" />
              </div>
            ) : preview ? (
              <PreviewBody p={preview} onRecheck={load} />
            ) : null}
            {error ? (
              <p role="alert" className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-fg">
                <CircleAlert className="mt-1 size-4 shrink-0 text-danger" />
                <span>{error}</span>
              </p>
            ) : null}
            {ready ? (
              <Field label={`برای تأیید بنویسید: ${CONFIRM_TEXT}`} htmlFor="tg-clean-confirm" hint="این کار برگشت‌پذیر نیست.">
                <TextInput
                  id="tg-clean-confirm"
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={40}
                  placeholder={CONFIRM_TEXT}
                />
              </Field>
            ) : null}
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-start">
              <Button variant="secondary" disabled={busy} onClick={() => setOpen(false)}>
                انصراف
              </Button>
              <Button type="submit" variant="danger" loading={busy} disabled={!ready || !confirmMatches(typed)} icon={<Trash2 className="size-4" />}>
                پاک کن
              </Button>
            </div>
          </form>
        )}
      </Modal>
    </>
  );
}

function PreviewBody({ p, onRecheck }: { p: TestChannelPreview; onRecheck: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      {p.channel ? (
        <div className="rounded-2xl border border-line bg-surface-2 p-3">
          <div className="text-sm font-bold text-fg">{p.channel.title || "کانال آزمایشی"}</div>
          <div className="ltr truncate text-right text-xs text-fg-muted">{p.channel.username}</div>
          {p.message_ids ? (
            <p className="mt-2 text-[0.84rem] leading-7 text-fg-muted">
              پیام‌های {toFaDigits(p.message_ids.first)} تا {toFaDigits(p.message_ids.last)} در {formatNumber(p.requests)} درخواست؛{" "}
              {formatNumber(p.rows.channel_posts)} پست شناخته‌شده‌ی پنل در این کانال.
            </p>
          ) : null}
        </div>
      ) : null}
      {p.blocked ? (
        <div role="alert" className="flex gap-2 rounded-2xl border border-warning/30 bg-warning-soft p-3 text-sm leading-7 text-fg">
          <ShieldAlert className="mt-1 size-4 shrink-0 text-warning" />
          <div className="flex flex-col gap-2">
            <span>{p.blocked_fa}</span>
            {p.blocked === "bot_cannot_delete" ? (
              <span className="text-[0.82rem] text-fg-muted">
                در تلگرام: کانال آزمایشی ← Administrators ← ربات ← «Delete messages» را روشن کنید. این اجازه را فقط در کانال آزمایشی بدهید، هرگز در کانال اصلی.
              </span>
            ) : null}
            {p.blocked !== "bot_not_set" && p.blocked !== "test_channel_not_set" && p.blocked !== "test_is_main" ? (
              <Button variant="secondary" size="sm" icon={<RefreshCw className="size-4" />} onClick={onRecheck} className="self-start">
                بررسی دوباره
              </Button>
            ) : null}
          </div>
        </div>
      ) : (
        <ul className="flex flex-col gap-1.5 rounded-2xl border border-danger/25 bg-danger-soft p-3 text-[0.84rem] leading-7 text-fg">
          <li>فقط کانال آزمایشی پاک میشود؛ کانال اصلی هرگز.</li>
          <li>پست‌های حساب دوم هم پاک میشوند، چون ربات در این کانال اجازه‌ی «حذف پیام‌ها» دارد.</li>
          <li>تلگرام پیامی را که اجازه‌ی حذفش را به ربات نمیدهد (طبق مستندات تلگرام، پیام قدیمی‌تر از ۴۸ ساعت) نگه میدارد؛ آن را دستی پاک کنید.</li>
          <li>پس از پاک شدن، اجازه‌ی «حذف پیام‌ها» را از ربات بگیرید.</li>
        </ul>
      )}
    </div>
  );
}

function Done({ result, onClose }: { result: TestChannelResult; onClose: () => void }) {
  return (
    <div className="flex flex-col gap-4">
      <p
        role="status"
        className={
          result.finished
            ? "flex gap-2 rounded-2xl bg-success-soft p-3 text-sm leading-7 text-fg"
            : "flex gap-2 rounded-2xl bg-warning-soft p-3 text-sm leading-7 text-fg"
        }
      >
        {result.finished ? <CircleCheck className="mt-1 size-4 shrink-0 text-success" /> : <ShieldAlert className="mt-1 size-4 shrink-0 text-warning" />}
        <span>
          {cleanSummary(result)}
          {result.stop_fa ? ` ${result.stop_fa}` : ""}
        </span>
      </p>
      <p className="flex gap-2 rounded-2xl border border-warning/30 bg-surface-2 p-3 text-[0.84rem] leading-7 text-fg">
        <ShieldAlert className="mt-1 size-4 shrink-0 text-warning" />
        <span>
          حالا در تلگرام، در تنظیمات ادمین‌های کانال آزمایشی، «حذف پیام‌ها» را برای ربات خاموش کنید و در همین صفحه «وارسی دوباره» را بزنید.
        </span>
      </p>
      <Button onClick={onClose} className="self-start">
        بستن
      </Button>
    </div>
  );
}
