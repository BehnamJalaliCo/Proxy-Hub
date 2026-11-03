import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Cloud, CloudCog, Globe, KeyRound, ListTree, ShieldCheck } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type SubmitEvent } from "react";

import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { ConfirmDialog } from "../components/ui/Dialogs";
import { EmptyState, LoadError, Skeleton } from "../components/ui/Feedback";
import { Field, SecretInput } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorList, errorMessage, isApiError } from "../lib/api";
import { cn } from "../lib/browser";
import { cfTokenProblem, poolChangeText, sortZones, ttlText } from "../lib/cloudflare";
import * as api from "../lib/endpoints";
import { formatJalali, formatNumber, formatRelative } from "../lib/format";
import { POOL_ORDER, poolStateInfo, zoneStatusInfo } from "../lib/labels";
import { keys, useCloudflareStatus } from "../lib/queries";
import { toast } from "../lib/toast";
import type { CfRecord, CfStatus, CfZone, PoolState } from "../lib/types";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

// --- token ---------------------------------------------------------------------------------------

/** The token lives only in this form's state; it is sent once and always cleared. Never cached. */
function TokenCard({ s }: { s: CfStatus }) {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [open, setOpen] = useState(!s.token_set);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ field: boolean; text: string; missing: string[] } | null>(null);

  const submit = (e: SubmitEvent) => {
    e.preventDefault();
    if (busy) return;
    const problem = cfTokenProblem(token);
    if (problem) {
      setError({ field: true, text: problem, missing: [] });
      return;
    }
    setBusy(true);
    setError(null);
    api
      .putCloudflareToken(token.trim())
      .then((status) => {
        qc.setQueryData(keys.cloudflare, status);
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("توکن Cloudflare ثبت شد", `${formatNumber(status.zones.length)} دامنه پیدا شد.`);
        setOpen(false);
      })
      .catch((err: unknown) => {
        const code = isApiError(err) ? err.code : "";
        setError({ field: code === "cf_token_invalid", text: errorMessage(err), missing: errorList(err, "missing") });
      })
      .finally(() => {
        // A token is never kept for another try, accepted or not.
        setToken("");
        setBusy(false);
      });
  };

  const state = !s.token_set
    ? { tone: "warning" as const, label: "وارد نشده" }
    : s.verified
      ? { tone: "success" as const, label: "وارسی‌شده" }
      : { tone: "danger" as const, label: "وارسی نشد" };

  return (
    <Card className="lg:col-span-5">
      <CardHeader
        icon={<KeyRound className="size-5" />}
        title="توکن Cloudflare"
        subtitle="فقط دو دسترسی: Zone Read و DNS Edit، و فقط روی یک دامنه: دامنه‌ی پروکسی. پنل دامنه‌های دیگرتان را نمی‌بیند."
        action={
          <Badge tone={state.tone} dot>
            {state.label}
          </Badge>
        }
      />
      <div className="flex flex-col gap-4 p-5">
        {s.token_set ? (
          <p className="flex items-center gap-2 rounded-2xl border border-line bg-surface-2 p-3 text-sm text-fg">
            <ShieldCheck className={cn("size-5 shrink-0", s.verified ? "text-success" : "text-danger")} />
            <span>
              توکن در گاوصندوق پنل است.{" "}
              <span className="text-fg-muted">
                آخرین وارسی: {s.checked_at ? `${formatRelative(s.checked_at)} · ${formatJalali(s.checked_at, "short")}` : "هنوز نه"}
              </span>
            </span>
          </p>
        ) : null}

        {s.token_set && !open ? (
          <Button variant="secondary" size="sm" className="self-start" icon={<KeyRound className="size-4" />} onClick={() => setOpen(true)}>
            عوض کردن توکن
          </Button>
        ) : (
          <form onSubmit={submit} noValidate autoComplete="off" className="flex flex-col gap-3">
            {!s.token_set ? (
              <ol className="flex list-inside list-[persian] flex-col gap-1 text-[0.84rem] leading-7 text-fg-muted marker:font-bold marker:text-brand">
                <li>در Cloudflare به My Profile و بعد API Tokens بروید و Create Token را بزنید.</li>
                <li>
                  دو دسترسی بدهید: <b className="ltr inline-block">Zone · Zone · Read</b> و <b className="ltr inline-block">Zone · DNS · Edit</b>.
                </li>
                <li>
                  در Zone Resources: <b className="ltr inline-block">Include · Specific zone</b> و فقط دامنه‌ی پروکسی را انتخاب کنید. توکنی که
                  بیش از یک دامنه را ببیند، پذیرفته نمیشود.
                </li>
                <li>توکن را اینجا بچسبانید. پنل همان یک دامنه را پیدا میکند.</li>
              </ol>
            ) : null}
            <Field label="توکن API" htmlFor="cf-token" hint="پس از ذخیره، توکن هرگز دوباره نشان داده نمیشود." error={error?.field ? error.text : null}>
              <SecretInput
                id="cf-token"
                name="cloudflare-token"
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
                allowReveal={false}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                invalid={Boolean(error?.field)}
                placeholder="••••••••••••••••••••••••••••••••••••••••"
              />
            </Field>
            <AnimatePresence>
              {error && !error.field ? (
                <motion.div
                  role="alert"
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-fg"
                >
                  <CircleAlert className="mt-1 size-4 shrink-0 text-danger" />
                  <div>
                    {error.text}
                    {error.missing.length > 0 ? (
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <span className="text-fg-muted">دسترسی‌های کم:</span>
                        {error.missing.map((m) => (
                          <code key={m} className="ltr rounded-md bg-surface px-1.5 py-0.5 text-xs font-semibold text-danger">
                            {m}
                          </code>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </motion.div>
              ) : null}
            </AnimatePresence>
            <div className="flex flex-col-reverse gap-2 sm:flex-row">
              {s.token_set ? (
                <Button
                  variant="secondary"
                  disabled={busy}
                  onClick={() => {
                    setOpen(false);
                    setToken("");
                    setError(null);
                  }}
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

// --- zones and the domain pool ---------------------------------------------------------------------

function ZoneRow({ zone, index, onPick }: { zone: CfZone; index: number; onPick: (target: PoolState) => void }) {
  const pool = poolStateInfo(zone.pool_state);
  const cf = zoneStatusInfo(zone.status);
  return (
    <motion.li {...fadeUp(index)} className={cn("flex flex-col gap-3 rounded-2xl border p-3.5 sm:flex-row sm:items-center", zone.pool_state === "active" ? "border-success/30 bg-success-soft/40" : "border-line bg-surface-2/50")}>
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <span className={cn("grid size-10 shrink-0 place-items-center rounded-xl", zone.pool_state === "active" ? "bg-success text-white" : "bg-surface-3 text-fg-muted")}>
          <Globe className="size-5" />
        </span>
        <div className="min-w-0">
          <div className="ltr truncate text-right font-bold text-fg" title={zone.name}>
            {zone.name}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-1.5">
            <Badge tone={pool.tone} dot>
              {pool.label}
            </Badge>
            <Badge tone={cf.tone}>{cf.label}</Badge>
          </div>
        </div>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <span className="shrink-0 text-fg-muted">جای در استخر</span>
        <select
          value={zone.pool_state}
          onChange={(e) => onPick(e.target.value as PoolState)}
          className="h-10 min-w-0 flex-1 rounded-xl border border-line bg-surface px-3 text-base font-semibold text-fg outline-none focus:border-brand sm:w-44 sm:flex-none sm:text-sm"
          aria-label={`جای ${zone.name} در استخر دامنه`}
        >
          {POOL_ORDER.map((p) => (
            <option key={p} value={p}>
              {poolStateInfo(p).label}
            </option>
          ))}
        </select>
      </label>
    </motion.li>
  );
}

function ZonesCard({ s }: { s: CfStatus }) {
  const qc = useQueryClient();
  const [pending, setPending] = useState<{ zone: CfZone; target: PoolState } | null>(null);
  const [busy, setBusy] = useState(false);
  const zones = sortZones(s.zones);

  const apply = () => {
    if (!pending) return;
    setBusy(true);
    api
      .putCloudflarePool(pending.zone.id, pending.target)
      .then((status) => {
        qc.setQueryData(keys.cloudflare, status);
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("استخر دامنه به‌روز شد", `«${pending.zone.name}»: ${poolStateInfo(pending.target).label}`);
        setPending(null);
      })
      .catch((e: unknown) => toast.error("تغییر انجام نشد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <Card className="lg:col-span-7">
      <CardHeader
        icon={<Cloud className="size-5" />}
        title="دامنه‌ها و استخر دامنه"
        subtitle="یک دامنه «فعال» است (نام‌های پروکسی پست‌های تازه). «آماده‌به‌کار» اگر دامنه‌ی فعال فیلتر شد، جایش را میگیرد."
      />
      <div className="p-4 sm:p-5">
        {zones.length === 0 ? (
          <EmptyState
            icon={<Globe className="size-7" />}
            title={s.token_set ? "دامنه‌ای پیدا نشد" : "هنوز توکنی وارد نشده"}
            description={
              s.token_set
                ? "دامنه را در Cloudflare اضافه کنید و nameserverهای Cloudflare را در ثبت‌کننده‌ی دامنه بگذارید؛ پنل خودش آن را پیدا میکند."
                : "پس از ثبت توکن، دامنه‌های حساب Cloudflare اینجا دیده میشوند."
            }
          />
        ) : (
          <ul className="flex flex-col gap-2.5">
            {zones.map((z, i) => (
              <ZoneRow
                key={z.id}
                zone={z}
                index={i}
                onPick={(target) => {
                  if (target !== z.pool_state) setPending({ zone: z, target });
                }}
              />
            ))}
          </ul>
        )}
      </div>
      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(v) => {
          if (!v && !busy) setPending(null);
        }}
        title={pending ? `«${pending.zone.name}»: ${poolStateInfo(pending.target).label}` : ""}
        description={pending ? poolChangeText(pending.zone, pending.target, s.zones) : ""}
        confirmLabel="تغییر بده"
        tone={pending?.target === "filtered" || pending?.target === "retired" ? "danger" : "primary"}
        icon={<CloudCog className="size-6" />}
        busy={busy}
        onConfirm={apply}
      />
    </Card>
  );
}

// --- managed records ------------------------------------------------------------------------------------

function RecordsCard({ records }: { records: CfRecord[] }) {
  const sorted = [...records].sort((a, b) => a.name.localeCompare(b.name, "en"));
  return (
    <Card className="lg:col-span-12">
      <CardHeader
        icon={<ListTree className="size-5" />}
        title="رکوردهای مدیریت‌شده"
        subtitle="فقط رکوردهایی که خود پنل ساخته (برچسب managed-by=proxy-hub). همه DNS-only (ابر خاکستری) با TTL کوتاه، تا IP پشت نام زود عوض شود."
        action={<Badge tone="brand">{formatNumber(records.length)} رکورد</Badge>}
      />
      <div className="p-3 sm:p-5">
        {sorted.length === 0 ? (
          <EmptyState icon={<ListTree className="size-7" />} title="هنوز رکوردی ساخته نشده" description="با آماده شدن nodeها، پنل برای هر جایگاه یک نام ثابت روی دامنه‌ی فعال میسازد." />
        ) : (
          <>
            {/* Phone: one card per record. */}
            <ul className="flex flex-col gap-2 md:hidden">
              {sorted.map((r) => (
                <li key={`${r.name}-${r.type}`} className="rounded-2xl border border-line bg-surface-2/50 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="ltr min-w-0 truncate text-right font-mono text-sm font-bold text-fg">{r.name}</span>
                    <Badge tone="neutral">{r.type}</Badge>
                  </div>
                  <div className="ltr mt-1 truncate text-right font-mono text-xs text-fg-muted">{r.content}</div>
                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-muted">
                    <span>TTL: {ttlText(r.ttl)}</span>
                    {r.proxied ? (
                      <span className="font-semibold text-danger">ابر نارنجی: باید خاکستری (DNS-only) باشد</span>
                    ) : (
                      <span>ابر خاکستری (DNS-only)</span>
                    )}
                    <span>{r.updated_at ? `به‌روز ${formatRelative(r.updated_at)}` : "—"}</span>
                  </div>
                </li>
              ))}
            </ul>
            {/* Larger screens: a table. */}
            <div className="hidden overflow-x-auto rounded-2xl border border-line md:block">
              <table className="w-full text-sm">
                <thead className="bg-surface-2 text-xs text-fg-muted">
                  <tr>
                    <th scope="col" className="px-4 py-2.5 text-start font-semibold">نام</th>
                    <th scope="col" className="px-3 py-2.5 text-start font-semibold">نوع</th>
                    <th scope="col" className="px-3 py-2.5 text-start font-semibold">مقدار</th>
                    <th scope="col" className="px-3 py-2.5 text-start font-semibold">TTL</th>
                    <th scope="col" className="px-3 py-2.5 text-start font-semibold">پروکسی Cloudflare</th>
                    <th scope="col" className="px-4 py-2.5 text-start font-semibold">آخرین تغییر</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {sorted.map((r) => (
                    <tr key={`${r.name}-${r.type}`} className="hover:bg-surface-2/60">
                      <td className="ltr px-4 py-2.5 text-right font-mono font-semibold text-fg">{r.name}</td>
                      <td className="px-3 py-2.5">
                        <Badge tone="neutral">{r.type}</Badge>
                      </td>
                      <td className="ltr max-w-64 truncate px-3 py-2.5 text-right font-mono text-fg-muted" title={r.content}>
                        {r.content}
                      </td>
                      <td className="px-3 py-2.5 text-fg">{ttlText(r.ttl)}</td>
                      <td className="px-3 py-2.5">
                        <Badge tone={r.proxied ? "danger" : "success"}>
                          {r.proxied ? "نارنجی: باید خاکستری باشد" : "خاکستری (DNS-only)"}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5 text-fg-muted" title={r.updated_at ? formatJalali(r.updated_at, "datetime") : undefined}>
                        {r.updated_at ? formatRelative(r.updated_at) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}

// --- page --------------------------------------------------------------------------------------------------

export default function CloudflarePage() {
  const status = useCloudflareStatus();
  const s = status.data;
  return (
    <>
      <PageHeader
        title="Cloudflare و دامنه‌ها"
        subtitle="پست‌ها هرگز IP ندارند، فقط نام. IP پشت نام عوض میشود و اگر دامنه فیلتر شد، دامنه‌ی آماده‌به‌کار جایش را میگیرد."
      />
      {status.isPending ? (
        <div className="grid gap-4 lg:grid-cols-12">
          <Skeleton className="h-64 rounded-3xl lg:col-span-5" />
          <Skeleton className="h-64 rounded-3xl lg:col-span-7" />
          <Skeleton className="h-48 rounded-3xl lg:col-span-12" />
        </div>
      ) : status.isError ? (
        <Card>
          <LoadError
            error={status.error}
            onRetry={() => void status.refetch()}
            notYet={{
              title: "بخش Cloudflare هنوز روی پنل فعال نشده",
              description:
                "ثبت توکن Cloudflare، استخر دامنه (فعال، آماده‌به‌کار، فیلترشده) و رکوردهای DNS مدیریت‌شده با به‌روزرسانی بعدی پنل فعال میشوند. کاری از شما لازم نیست.",
            }}
          />
        </Card>
      ) : s ? (
        <div className="grid gap-4 sm:gap-5 lg:grid-cols-12">
          <TokenCard s={s} />
          <ZonesCard s={s} />
          <RecordsCard records={s.records} />
        </div>
      ) : null}
    </>
  );
}
