import { useQueryClient } from "@tanstack/react-query";
import { Archive, CircleAlert, Eraser, Info } from "lucide-react";
import { useState } from "react";

import { errorMessage } from "../lib/api";
import { keptLines, MIN_AGE_OPTIONS, previewEndedPurge, purgeEnded, purgeLines, type EndedPurge } from "../lib/cleanup";
import { formatNumber } from "../lib/format";
import { toast } from "../lib/toast";
import { Button } from "./ui/Button";
import { ConfirmDialog } from "./ui/Dialogs";
import { Skeleton } from "./ui/Feedback";
import { Segmented } from "./ui/Form";

/**
 * «پاک کردن پایان‌یافته‌ها»: removes ENDED rows from the panel's database (owner's button). The
 * dialog first shows the server's dry-run counts for the chosen minimum age; the purge runs only
 * after the owner confirms, in one transaction on the server, and is written to «رویدادها».
 */
export function PurgeEndedButton({ size = "sm", label = "پاک کردن پایان‌یافته‌ها" }: { size?: "sm" | "md"; label?: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [age, setAge] = useState(MIN_AGE_OPTIONS[0]?.value ?? "1");
  const [preview, setPreview] = useState<EndedPurge | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const hoursOf = (value: string) => MIN_AGE_OPTIONS.find((o) => o.value === value)?.hours ?? 1;

  const load = (value: string) => {
    setLoading(true);
    setError(null);
    previewEndedPurge(hoursOf(value))
      .then(setPreview)
      .catch((e: unknown) => {
        setPreview(null);
        setError(errorMessage(e));
      })
      .finally(() => setLoading(false));
  };

  const openDialog = () => {
    setOpen(true);
    load(age);
  };

  const confirm = () => {
    setBusy(true);
    purgeEnded(hoursOf(age))
      .then((r) => {
        toast.success(
          "پایان‌یافته‌ها پاک شدند",
          r.total ? `${formatNumber(r.total)} ردیف پایان‌یافته از دیتابیس پنل پاک شد.` : "چیزی برای پاک کردن نبود.",
        );
        void qc.invalidateQueries();
        setOpen(false);
      })
      .catch((e: unknown) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const lines = preview ? purgeLines(preview.removed) : [];
  const kept = preview ? keptLines(preview.kept) : [];

  return (
    <>
      <Button variant="secondary" size={size} icon={<Eraser className="size-4" />} onClick={openDialog}>
        {label}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={(v) => {
          if (!busy) setOpen(v);
        }}
        title="پاک کردن پایان‌یافته‌ها"
        description="فقط آنچه تمام شده از دیتابیس پنل پاک میشود: nodeهای حذف‌شده با سابقه‌شان، کانفیگ‌های Reality پایان‌یافته، ثبت‌های اسپانسر بازنشسته، ردیف‌های پست‌های تمام‌شده‌ی کانال آزمایشی و کارهای تمام‌شده‌ی صف ارسال. چیزی از تلگرام، هتزنر یا Cloudflare پاک نمیشود."
        confirmLabel={preview && preview.total > 0 ? `پاک کن (${formatNumber(preview.total)} ردیف)` : "پاک کن"}
        tone="danger"
        icon={<Archive className="size-6" />}
        busy={busy}
        confirmDisabled={loading || !preview || preview.total === 0}
        onConfirm={confirm}
      >
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-semibold text-fg">فقط آنچه دست‌کم این مدت پیش تمام شده</span>
            <Segmented
              value={age}
              onChange={(v) => {
                setAge(v);
                load(v);
              }}
              options={MIN_AGE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
              label="کمینه‌ی عمر"
              className="w-full"
            />
          </div>
          {loading ? (
            <div className="flex flex-col gap-2" aria-busy>
              <Skeleton className="h-6 w-2/3" />
              <Skeleton className="h-6 w-1/2" />
              <Skeleton className="h-6 w-3/5" />
            </div>
          ) : error ? (
            <p role="alert" className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-fg">
              <CircleAlert className="mt-1 size-4 shrink-0 text-danger" />
              <span>{error}</span>
            </p>
          ) : preview ? (
            preview.total === 0 ? (
              <p className="rounded-2xl border border-line bg-surface-2 p-3 text-sm leading-7 text-fg-muted">
                با این کمینه‌ی عمر، چیزی برای پاک کردن نیست.
              </p>
            ) : (
              <ul className="flex flex-col divide-y divide-line rounded-2xl border border-line bg-surface-2" aria-label="آنچه پاک میشود">
                {lines.map((l) => (
                  <li key={l.key} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                    <span className="text-fg">{l.label}</span>
                    <span className="num font-bold text-fg">{formatNumber(l.count)}</span>
                  </li>
                ))}
              </ul>
            )
          ) : null}
          {!loading && preview && (kept.length > 0 || preview.ip_history_unlinked > 0) ? (
            <div className="flex gap-2 rounded-2xl border border-line bg-surface p-3 text-[0.82rem] leading-6 text-fg-muted">
              <Info className="mt-1 size-4 shrink-0 text-brand" />
              <ul className="flex flex-col gap-1">
                {kept.map((k) => (
                  <li key={k}>{k}</li>
                ))}
                {preview.ip_history_unlinked > 0 ? (
                  <li>حافظه‌ی IPهای سوخته میماند تا همان IPها دوباره گرفته نشوند.</li>
                ) : null}
                <li>دفتر هزینه، رویدادها، تنظیمات و رمزها هرگز پاک نمیشوند.</li>
              </ul>
            </div>
          ) : null}
        </div>
      </ConfirmDialog>
    </>
  );
}
