// «فهرست ‎@MTProxybot‎» on the sponsor page: which proxies the operator account has registered in
// @MTProxybot, which of them are ours (live, server deleted, unknown), the sponsor each live one shows,
// and the owner's two buttons: read the list, and clean the dead ones out of it (panel/proxyhub/sponsor/
// inventory.py). No secret reaches the browser: addresses, label shapes and screen shapes only.

import { CircleStop, Eraser, ListChecks, ScanSearch, ScrollText } from "lucide-react";
import { useState, type ReactNode } from "react";

import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import { formatJalali, formatNumber, formatRelative } from "../lib/format";
import {
  botEntryStatusInfo,
  botJobInfo,
  botEntryNoteText,
  botJobOpen,
  botPromotionInfo,
  botRemovalInfo,
  botScreenStage,
  channelText,
  sortBotEntries,
  startBotListCleanup,
  startBotListInventory,
  stopBotListJob,
  type BotListSection,
  type SponsorOverview,
} from "../lib/sponsor";
import { toast } from "../lib/toast";
import { TONE_SOFT, TONE_TEXT } from "../lib/tones";
import { Button } from "./ui/Button";
import { Badge, Card, CardHeader, StatusDot } from "./ui/Card";
import { ConfirmDialog } from "./ui/Dialogs";
import { EmptyState } from "./ui/Feedback";

type Action = "inventory" | "cleanup" | "stop";

function Count({ label, value, tone }: { label: string; value: number; tone: "neutral" | "success" | "danger" | "warning" }) {
  return (
    <div className={cn("min-w-0 rounded-2xl border p-3", tone === "neutral" ? "border-line bg-surface-2" : cn("border-transparent", TONE_SOFT[tone]))}>
      <div className="text-xs text-fg-muted">{label}</div>
      <div className="mt-1 text-xl font-extrabold text-fg">{formatNumber(value)}</div>
    </div>
  );
}

function Screens({ s }: { s: BotListSection }) {
  if (s.screens.length === 0) return null;
  return (
    <details className="rounded-2xl border border-line bg-surface-2/50 p-3">
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-semibold text-fg">
        <ScrollText className="size-4 text-fg-muted" />
        شکل صفحه‌های ربات در آخرین کار ({formatNumber(s.screens.length)})
        <span className="text-xs font-normal text-fg-faint">برای گزارش فنی؛ نشانی‌ها و رشته‌های hex جایگزین شده‌اند و secretی در آن نیست.</span>
      </summary>
      <ol className="mt-3 flex flex-col gap-2.5">
        {s.screens.map((screen, i) => (
          <li key={i} className="rounded-xl border border-line bg-surface p-2.5">
            <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs text-fg-muted">
              <Badge tone="neutral">{botScreenStage(screen.stage)}</Badge>
              <span className="ltr inline-block font-mono">{screen.about}</span>
              {screen.at ? <span>{formatRelative(screen.at)}</span> : null}
            </div>
            {screen.text ? (
              <pre dir="ltr" className="max-h-40 overflow-auto rounded-lg bg-surface-2 p-2 text-left font-mono text-xs leading-5 whitespace-pre-wrap text-fg">
                {screen.text}
              </pre>
            ) : null}
            {screen.answer ? (
              <div dir="ltr" className="mt-1 text-left font-mono text-xs text-fg-muted">
                toast: {screen.answer}
              </div>
            ) : null}
            {screen.buttons.length > 0 ? (
              <div dir="ltr" className="mt-1.5 flex flex-col gap-1">
                {screen.buttons.map((row, r) => (
                  <div key={r} className="flex flex-wrap gap-1">
                    {row.map((label, b) => (
                      <span key={b} className="rounded-lg border border-line-strong bg-surface-2 px-2 py-0.5 font-mono text-xs text-fg">
                        {label}
                      </span>
                    ))}
                  </div>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  );
}

function Table({ s }: { s: BotListSection }) {
  const entries = sortBotEntries(s.entries);
  return (
    <div className="overflow-x-auto rounded-2xl border border-line">
      <table className="w-full border-separate border-spacing-0 text-sm">
        <caption className="sr-only">پروکسی‌هایی که حساب دوم در ‎@MTProxybot‎ ثبت کرده است</caption>
        <thead>
          <tr>
            {["نشانی در ‎@MTProxybot‎", "وضعیت", "کانال اسپانسر"].map((h) => (
              <th key={h} scope="col" className="bg-surface-2 px-3 py-2.5 text-start text-xs font-semibold whitespace-nowrap text-fg-muted">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => {
            const st = botEntryStatusInfo(e.status);
            const promo = botPromotionInfo(e, s.wanted_channel);
            const removal = botRemovalInfo(e, s.list?.read_at ?? null);
            return (
              <tr key={e.id}>
                <th scope="row" className="border-t border-line px-3 py-2 text-start align-top font-normal">
                  <div className="ltr text-right font-mono text-[0.85rem] font-bold text-fg">{e.address}</div>
                  <div className="ltr text-right font-mono text-[0.7rem] text-fg-faint" title="شکل برچسب دکمه در ربات (hex جایگزین شده)">
                    {e.label_shape}
                  </div>
                  {e.listed > 1 ? <div className="text-[0.7rem] text-warning">{formatNumber(e.listed)} بار در فهرست</div> : null}
                </th>
                <td className="border-t border-line px-3 py-2 align-top">
                  <Badge tone={st.tone} dot>
                    <span title={st.hint}>{st.label}</span>
                  </Badge>
                  {e.removable ? <div className="mt-1 text-[0.72rem] text-fg-muted">پاک‌شدنی</div> : null}
                  {botEntryNoteText(e.note) ? (
                    <div className="mt-1 text-[0.72rem] leading-5 text-fg-muted">{botEntryNoteText(e.note)}</div>
                  ) : null}
                  {e.status === "gone" && !e.removable ? (
                    <div className="mt-1 text-[0.72rem] text-fg-muted">از دکمه‌های هم‌نام قابل تشخیص نیست؛ پاک نمیشود.</div>
                  ) : null}
                  {removal ? <div className={cn("mt-1 text-[0.72rem] leading-5", TONE_TEXT[removal.tone])}>{removal.text}</div> : null}
                </td>
                <td className="border-t border-line px-3 py-2 align-top">
                  {promo ? (
                    <span className={cn("text-[0.8rem] leading-6", TONE_TEXT[promo.tone])}>{promo.text}</span>
                  ) : (
                    <span className="text-fg-faint">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function BotListCard({ s, onApply }: { s: BotListSection; onApply: (o: SponsorOverview) => void }) {
  const [confirm, setConfirm] = useState<"cleanup" | null>(null);
  const [busy, setBusy] = useState<Action | null>(null);
  const job = botJobInfo(s);
  const open = botJobOpen(s);
  const removing = s.cleanup_mode === "remove";

  const run = (action: Action) => {
    setBusy(action);
    const call = action === "inventory" ? startBotListInventory() : action === "cleanup" ? startBotListCleanup() : stopBotListJob();
    call
      .then((o) => {
        onApply(o);
        setConfirm(null);
        toast.success(
          action === "stop" ? "متوقف شد" : "در صف است",
          action === "stop" ? undefined : "پنل در گذر بعدی (حداکثر یک دقیقه، وقتی کار پروکسی‌های زنده نباشد) شروع میکند.",
        );
      })
      .catch((err: unknown) => toast.error("انجام نشد", errorMessage(err)))
      .finally(() => setBusy(null));
  };

  const problem = s.counts.promotion_problem;
  let promotionNote: ReactNode = null;
  if (s.counts.live > 0) {
    promotionNote =
      problem > 0 ? (
        <p className={cn("rounded-2xl px-3 py-2 text-sm leading-7", TONE_SOFT.danger)}>
          {formatNumber(problem)} پروکسی فعال اسپانسر {channelText(s.wanted_channel)} را نشان نمیدهد (ستون «کانال اسپانسر»).
        </p>
      ) : s.counts.promotion_ok === s.counts.live ? (
        <p className={cn("rounded-2xl px-3 py-2 text-sm leading-7", TONE_SOFT.success)}>
          همه‌ی پروکسی‌های فعال در ‎@MTProxybot‎ اسپانسر {channelText(s.wanted_channel)} را نشان میدهند.
        </p>
      ) : null;
  }

  return (
    <Card className="lg:col-span-12">
      <CardHeader
        icon={<ListChecks className="size-5" />}
        title="فهرست ‎@MTProxybot‎"
        subtitle="پروکسی‌هایی که حساب دوم در ‎@MTProxybot‎ ثبت کرده است، اینکه هر کدام مال کدام سرور ماست، و اسپانسری که ربات برای هر پروکسی فعال نشان میدهد."
        action={s.list ? <Badge tone="brand">{formatNumber(s.list.total)} پروکسی</Badge> : undefined}
      />
      <div className="flex flex-col gap-4 p-4 sm:p-5">
        {job ? (
          <div className={cn("flex items-start gap-2.5 rounded-2xl p-3 text-sm leading-7", TONE_SOFT[job.tone])} role="status">
            <StatusDot tone={job.tone} pulse={open} className="mt-2.5" />
            <div className="min-w-0">
              <b className="text-fg">{job.title}</b>
              {job.text ? <p className="text-fg-muted">{job.text}</p> : null}
            </div>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" icon={<ScanSearch className="size-4" />} loading={busy === "inventory"} disabled={open || busy !== null} onClick={() => run("inventory")}>
            خواندن فهرست ‎@MTProxybot‎
          </Button>
          <Button variant={removing ? "danger" : "primary"} icon={<Eraser className="size-4" />} disabled={open || busy !== null} onClick={() => setConfirm("cleanup")}>
            پاک کردن پروکسی‌های مرده از ‎@MTProxybot‎
          </Button>
          {open ? (
            <Button variant="ghost" icon={<CircleStop className="size-4" />} loading={busy === "stop"} disabled={busy !== null} onClick={() => run("stop")}>
              توقف
            </Button>
          ) : null}
        </div>
        <p className="text-xs leading-6 text-fg-muted">
          {removing
            ? "حالت پاک کردن: حذف واقعی. فقط پروکسی‌هایی پاک میشوند که سرورشان حذف شده و ثبتشان در پنل هست؛ پروکسی فعال یا ناشناس هرگز."
            : "حالت پاک کردن: فقط مشاهده. پنل صفحه‌ی یک پروکسی مرده را میبیند و شکل دکمه‌هایش را ثبت میکند، بی آنکه چیزی بزند. حذف واقعی پس از بررسی همین شکل‌ها روشن میشود."}
          {s.observation ? ` آخرین مشاهده ${formatRelative(s.observation.at)}: ${s.observation.result === "remove_button_found" ? "یک دکمه‌ی حذف با شکل شناخته‌شده پیدا شد." : "دکمه‌ی حذف با شکل شناخته‌شده پیدا نشد."}` : ""}
        </p>
        {s.list === null ? (
          <EmptyState
            icon={<ListChecks className="size-7" />}
            title="هنوز فهرست خوانده نشده"
            description="«خواندن فهرست» را بزنید. پنل ‎/myproxies‎ را میفرستد، همه‌ی صفحه‌ها را میخواند و هر پروکسی را با سرورهای خودش جور میکند."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Count label="فعال" value={s.counts.live} tone="success" />
              <Count label="سرور حذف‌شده" value={s.counts.gone} tone={s.counts.gone > 0 ? "danger" : "neutral"} />
              <Count label="ناشناس" value={s.counts.unknown} tone={s.counts.unknown > 0 ? "warning" : "neutral"} />
              <Count label="بازنشسته؛ سرور هنوز هست" value={s.counts.retired} tone="neutral" />
            </div>
            {promotionNote}
            <p className="text-xs leading-6 text-fg-faint">
              خوانده‌شده {formatRelative(s.list.read_at)} ({formatJalali(s.list.read_at, "short")})، {formatNumber(s.list.pages)} صفحه
              {s.list.complete ? "" : "؛ همه‌ی صفحه‌ها خوانده نشد، پس چیزی پاک نمیشود"}.
            </p>
            {s.entries.length > 0 ? <Table s={s} /> : <p className="text-sm text-fg-muted">فهرست ربات خالی بود.</p>}
          </>
        )}
        <Screens s={s} />
      </div>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(v) => {
          if (!v && busy === null) setConfirm(null);
        }}
        title="پاک کردن پروکسی‌های مرده از ‎@MTProxybot‎"
        description={
          removing
            ? `پنل اول فهرست را دوباره میخواند. بعد هر پروکسی‌ای که سرورش حذف شده (اکنون ${formatNumber(s.counts.removable)} تا) را یکی‌یکی و آرام از ‎@MTProxybot‎ پاک میکند. پروکسی‌های فعال و ناشناس دست نمیخورند. اگر ربات صفحه‌ی ناآشنایی نشان دهد، همان‌جا میایستد.`
            : "در این اجرا چیزی پاک نمیشود. پنل فهرست را دوباره میخواند و صفحه‌ی یک پروکسی مرده را فقط میبیند تا شکل دکمه‌های حذف و تأیید ثبت شود."
        }
        confirmLabel={removing ? "پاک کن" : "شروع مشاهده"}
        tone={removing ? "danger" : "primary"}
        icon={<Eraser className="size-6" />}
        acknowledge={removing ? "میدانم پروکسی‌های پاک‌شده از ‎@MTProxybot‎ برنمیگردند." : undefined}
        busy={busy === "cleanup"}
        onConfirm={() => run("cleanup")}
      />
    </Card>
  );
}
