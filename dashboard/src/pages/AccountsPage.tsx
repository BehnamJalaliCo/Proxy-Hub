import { useQueryClient } from "@tanstack/react-query";
import { Archive, CalendarClock, CircleAlert, KeyRound, LogOut, Plus, Server, ServerCog, ShieldAlert } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type SubmitEvent } from "react";
import { useSearchParams } from "react-router";

import { Button } from "../components/ui/Button";
import { Badge, Card } from "../components/ui/Card";
import { ConfirmDialog, Modal } from "../components/ui/Dialogs";
import { EmptyState, ErrorState, Skeleton } from "../components/ui/Feedback";
import { Field, SecretInput, TextInput } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { cn } from "../lib/browser";
import { errorMessage, isApiError } from "../lib/api";
import * as api from "../lib/endpoints";
import { formatJalali, formatNumber, formatRelative } from "../lib/format";
import { accountActions, accountStateInfo, stateReasonText } from "../lib/labels";
import { keys, useAccounts } from "../lib/queries";
import { toast } from "../lib/toast";
import type { Account } from "../lib/types";

const STATE_ORDER = ["active", "verifying", "preparing", "draining", "locked", "retired"];

// --- add account --------------------------------------------------------------------------------

/** The token lives only in this form's state; it is sent once and cleared. Not in any cache. */
function AddAccountDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<{ field: "name" | "token" | null; text: string } | null>(null);

  const close = (v: boolean) => {
    if (!v) {
      setToken("");
      setErr(null);
    }
    onOpenChange(v);
  };

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const n = name.trim();
    if (n === "" || n.length > 100) {
      setErr({ field: "name", text: "نامی ۱ تا ۱۰۰ نویسه‌ای برای حساب بنویسید." });
      return;
    }
    if (token.trim() === "") {
      setErr({ field: "token", text: "توکن Read & Write حساب را بچسبانید." });
      return;
    }
    setBusy(true);
    setErr(null);
    api
      .addAccount(n, token.trim())
      .then((acc) => {
        setToken("");
        setName("");
        void qc.invalidateQueries({ queryKey: keys.accounts });
        void qc.invalidateQueries({ queryKey: keys.overview });
        toast.success("حساب افزوده شد", `«${acc.name}» با وضعیت «${accountStateInfo(acc.state).label}» ثبت شد.`);
        onOpenChange(false);
      })
      .catch((error: unknown) => {
        const code = isApiError(error) ? error.code : "";
        // A rejected token is never kept for another try.
        if (code !== "name_taken" && code !== "invalid_name") setToken("");
        let text = errorMessage(error);
        if (code === "account_not_empty" && isApiError(error)) {
          const n2 = error.num("foreign_servers");
          if (n2 !== null) text = `${text} (${formatNumber(n2)} سرور بیگانه)`;
          void qc.invalidateQueries({ queryKey: keys.accounts });
        }
        setErr({ field: code === "name_taken" || code === "invalid_name" ? "name" : code.startsWith("token") ? "token" : null, text });
      })
      .finally(() => setBusy(false));
  };

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="افزودن حساب هتزنر"
      description="توکن Read & Write یک project خالی و جدا برای nodeها. پس از ذخیره، توکن هرگز دوباره نشان داده نمیشود."
      icon={<KeyRound className="size-5" />}
    >
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate autoComplete="off">
        <Field label="نام حساب" htmlFor="acc-name" hint="برای شناختن حساب؛ مثلاً «حساب آبان ۱۴۰۵»." error={err?.field === "name" ? err.text : null}>
          <TextInput
            id="acc-name"
            value={name}
            maxLength={100}
            onChange={(e) => setName(e.target.value)}
            invalid={err?.field === "name"}
            placeholder="حساب ۱"
          />
        </Field>
        <Field
          label="توکن API"
          htmlFor="acc-token"
          hint="در کنسول هتزنر، در همان project بخش Security و سپس API tokens؛ توکنی با دسترسی Read & Write بسازید."
          error={err?.field === "token" ? err.text : null}
        >
          <SecretInput
            id="acc-token"
            name="hetzner-token"
            autoComplete="off"
            data-1p-ignore
            data-lpignore="true"
            allowReveal={false}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            invalid={err?.field === "token"}
            placeholder="••••••••••••••••"
          />
        </Field>
        <div className="flex gap-3 rounded-2xl border border-warning/25 bg-warning-soft p-3.5 text-[0.84rem] leading-7 text-fg">
          <ShieldAlert className="mt-1 size-5 shrink-0 text-warning" />
          <p>
            فقط توکن <b>project جدای nodeها</b> را وارد کنید. اگر در آن project سروری باشد که مال این سیستم نیست، توکن رد و حساب قفل
            میشود (قانون ۴).
          </p>
        </div>
        <AnimatePresence>
          {err && err.field === null ? (
            <motion.p
              role="alert"
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-fg"
            >
              <CircleAlert className="mt-1 size-4 shrink-0 text-danger" />
              {err.text}
            </motion.p>
          ) : null}
        </AnimatePresence>
        <div className="flex flex-col-reverse gap-2 sm:flex-row">
          <Button variant="secondary" onClick={() => close(false)} disabled={busy}>
            انصراف
          </Button>
          <Button type="submit" className="sm:flex-1" loading={busy} icon={<Plus className="size-5" />}>
            وارسی و افزودن
          </Button>
        </div>
        {busy ? <p className="text-center text-xs text-fg-muted">پنل توکن و project را روی هتزنر وارسی میکند…</p> : null}
      </form>
    </Modal>
  );
}

// --- one account ----------------------------------------------------------------------------------

function UsageBar({ used, limit }: { used: number | null; limit: number | null }) {
  if (used === null) return <span className="text-xs text-fg-faint">تعداد سرور هنوز خوانده نشده</span>;
  const ratio = limit && limit > 0 ? Math.min(1, used / limit) : null;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-fg-muted">سرورها</span>
        <span className="font-bold text-fg">
          {formatNumber(used)}
          {limit !== null ? <span className="font-normal text-fg-muted"> از سقف {formatNumber(limit)}</span> : <span className="font-normal text-fg-faint"> (سقف هنوز یاد گرفته نشده)</span>}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-brand-soft">
        {ratio !== null ? (
          <motion.div
            className={cn("h-full rounded-full", ratio >= 1 ? "bg-danger" : ratio >= 0.8 ? "bg-warning" : "bg-brand")}
            initial={{ width: 0 }}
            animate={{ width: `${String(ratio * 100)}%` }}
            transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
          />
        ) : null}
      </div>
    </div>
  );
}

function AccountCard({ account, index, onDrain, onRetire }: { account: Account; index: number; onDrain: () => void; onRetire: () => void }) {
  const info = accountStateInfo(account.state);
  const actions = accountActions(account.state);
  const reason = stateReasonText(account.state_reason);
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.04 * index, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
    >
      <Card className={cn("flex h-full flex-col gap-4 p-5", account.state === "locked" && "border-danger/30", account.state === "retired" && "opacity-75")}>
        <div className="flex items-start gap-3">
          <span
            className={cn(
              "grid size-11 shrink-0 place-items-center rounded-2xl",
              account.state === "locked" ? "bg-danger-soft text-danger" : account.state === "active" ? "bg-success-soft text-success" : "bg-surface-3 text-fg-muted",
            )}
          >
            {account.state === "locked" ? <ShieldAlert className="size-5" /> : <ServerCog className="size-5" />}
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-base font-bold text-fg" title={account.name}>
              {account.name}
            </h3>
            <p className="flex items-center gap-1 text-xs text-fg-faint">
              <CalendarClock className="size-3.5" />
              افزوده‌شده {formatJalali(account.created_at, "date")} · {formatRelative(account.created_at)}
            </p>
          </div>
          <Badge tone={info.tone} dot pulse={account.state === "verifying" || account.state === "preparing"}>
            {info.label}
          </Badge>
        </div>
        <UsageBar used={account.servers_in_account} limit={account.learned_server_limit} />
        {reason ? (
          <p className={cn("rounded-xl px-3 py-2 text-[0.82rem] leading-6", account.state === "locked" ? "bg-danger-soft text-fg" : "bg-surface-2 text-fg-muted")}>
            {reason}
          </p>
        ) : info.hint ? (
          <p className="text-[0.82rem] leading-6 text-fg-muted">{info.hint}</p>
        ) : null}
        {actions.drain || actions.retire ? (
          <div className="mt-auto flex flex-wrap gap-2 border-t border-line pt-4">
            {actions.drain ? (
              <Button variant="secondary" size="sm" icon={<LogOut className="size-4 rtl:-scale-x-100" />} onClick={onDrain}>
                تخلیه
              </Button>
            ) : null}
            {actions.retire ? (
              <Button variant="secondary" size="sm" className="text-danger" icon={<Archive className="size-4" />} onClick={onRetire}>
                بازنشسته کردن
              </Button>
            ) : null}
          </div>
        ) : null}
      </Card>
    </motion.li>
  );
}

// --- page -------------------------------------------------------------------------------------------

export default function AccountsPage() {
  const qc = useQueryClient();
  const accounts = useAccounts();
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(params.get("add") === "1");
  const [confirm, setConfirm] = useState<{ kind: "drain" | "retire"; account: Account } | null>(null);
  const [busy, setBusy] = useState(false);

  const list = [...(accounts.data ?? [])].sort(
    (a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state) || b.created_at.localeCompare(a.created_at),
  );
  const counts = new Map<string, number>();
  for (const a of list) counts.set(a.state, (counts.get(a.state) ?? 0) + 1);

  const runConfirm = () => {
    if (!confirm) return;
    setBusy(true);
    const call = confirm.kind === "drain" ? api.drainAccount(confirm.account.id) : api.retireAccount(confirm.account.id);
    call
      .then((acc) => {
        qc.setQueryData<Account[]>(keys.accounts, (old) => old?.map((a) => (a.id === acc.id ? acc : a)));
        void qc.invalidateQueries({ queryKey: keys.accounts });
        void qc.invalidateQueries({ queryKey: keys.overview });
        toast.success(confirm.kind === "drain" ? "تخلیه‌ی حساب شروع شد" : "حساب بازنشسته شد", `«${acc.name}»: ${accountStateInfo(acc.state).label}`);
        setConfirm(null);
      })
      .catch((e: unknown) => toast.error("انجام نشد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <PageHeader
        title="حساب‌های هتزنر"
        subtitle="nodeها فقط در این حساب‌های جدا ساخته میشوند؛ پنل هر یک تا دو ماه حساب تازه را جایگزین کهنه میکند."
        actions={
          <Button icon={<Plus className="size-5" />} onClick={() => setAdding(true)}>
            افزودن حساب
          </Button>
        }
      />

      {list.length > 0 ? (
        <div className="mb-5 flex flex-wrap gap-2">
          {STATE_ORDER.filter((s) => counts.get(s)).map((s) => (
            <Badge key={s} tone={accountStateInfo(s).tone} dot>
              {accountStateInfo(s).label}: {formatNumber(counts.get(s) ?? 0)}
            </Badge>
          ))}
        </div>
      ) : null}

      {accounts.isPending ? (
        <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <li key={i}>
              <Skeleton className="h-56 rounded-3xl" />
            </li>
          ))}
        </ul>
      ) : accounts.isError ? (
        <Card>
          <ErrorState error={accounts.error} onRetry={() => void accounts.refetch()} />
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Server className="size-7" />}
            title="هنوز حسابی افزوده نشده"
            description="یک حساب جدای هتزنر بسازید، در آن یک project خالی و یک توکن Read & Write بسازید و اینجا وارد کنید. حساب اصلی (سرور پنل) هرگز اینجا وارد نمیشود."
            action={
              <Button icon={<Plus className="size-5" />} onClick={() => setAdding(true)}>
                افزودن اولین حساب
              </Button>
            }
          />
        </Card>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map((a, i) => (
            <AccountCard
              key={a.id}
              account={a}
              index={i}
              onDrain={() => setConfirm({ kind: "drain", account: a })}
              onRetire={() => setConfirm({ kind: "retire", account: a })}
            />
          ))}
        </ul>
      )}

      <AddAccountDialog
        open={adding}
        onOpenChange={(v) => {
          setAdding(v);
          if (!v && params.has("add")) setParams({}, { replace: true });
        }}
      />

      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(v) => {
          if (!v && !busy) setConfirm(null);
        }}
        title={confirm?.kind === "drain" ? `تخلیه‌ی «${confirm.account.name}»` : `بازنشستگی «${confirm?.account.name ?? ""}»`}
        description={
          confirm?.kind === "drain"
            ? "از این پس هیچ node تازه‌ای در این حساب ساخته نمیشود و nodeهای موجود با پایان کانفیگ‌هایشان بازنشسته میشوند. این کار برگشت ندارد."
            : "توکن این حساب از گاوصندوق پنل پاک میشود و حساب دیگر استفاده نمیشود. فقط وقتی ممکن است که هیچ node زنده‌ای در آن نباشد. این کار برگشت ندارد."
        }
        confirmLabel={confirm?.kind === "drain" ? "تخلیه کن" : "بازنشسته کن"}
        tone={confirm?.kind === "drain" ? "primary" : "danger"}
        icon={confirm?.kind === "drain" ? <LogOut className="size-6 rtl:-scale-x-100" /> : <Archive className="size-6" />}
        busy={busy}
        onConfirm={runConfirm}
      />
    </>
  );
}
