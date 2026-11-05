import { MapPinned, TriangleAlert } from "lucide-react";

import { cn } from "../lib/browser";
import { formatNumber, formatRelative, toFaDigits } from "../lib/format";
import { closedAllowed, locationClassInfo, locationRow, networkLabel } from "../lib/iranReach";
import { TONE_SOFT } from "../lib/tones";
import type { LocationsSummary } from "../lib/types";
import { Badge } from "./ui/Card";
import { EmptyState } from "./ui/Feedback";

/** Hetzner location x Iranian network: did a TLS handshake to our nodes there work lately? */
export function LocationReachTable({ summary }: { summary: LocationsSummary }) {
  const keys = summary.key_networks;
  const closed = closedAllowed(summary.locations);
  const allClosed = closed.length > 0 && summary.locations.filter((l) => l.allowed).every((l) => l.class === "bad");
  const anyData = summary.locations.some((l) => l.networks.length > 0);
  return (
    <div className="flex flex-col gap-3">
      {allClosed ? (
        <div role="alert" className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-danger">
          <TriangleAlert className="mt-1 size-4 shrink-0" />
          همه‌ی locationهای مجاز از همراه اول و رایتل بسته‌اند؛ سرور تازه ساخته نمیشود تا location دیگری در «تنظیمات» اضافه کنید.
        </div>
      ) : closed.length > 0 ? (
        <p className="rounded-2xl bg-warning-soft px-3 py-2 text-sm leading-7 text-warning">
          <span className="ltr inline-block font-mono">{closed.join("، ")}</span> از همراه اول و رایتل بسته بوده؛ سرور تازه در آن ساخته نمیشود.
        </p>
      ) : null}
      {!anyData ? (
        <EmptyState
          icon={<MapPinned className="size-7" />}
          title="هنوز سنجشی نیست"
          description={`وقتی nodeهای ما از ایران با دست‌دادن TLS سنجیده شوند، اینجا دیده میشود هر location از کدام اپراتور باز است (${toFaDigits(String(summary.window_days))} روز اخیر).`}
        />
      ) : null}
      <div className="overflow-x-auto rounded-2xl border border-line">
        <table className="w-full border-separate border-spacing-0 text-sm">
          <caption className="sr-only">باز بودن هر location هتزنر از هر شبکه‌ی ایران در روزهای اخیر</caption>
          <thead>
            <tr>
              <th scope="col" className="sticky start-0 z-10 bg-surface-2 px-3 py-2.5 text-start text-xs font-semibold text-fg-muted">
                location
              </th>
              {keys.map((k) => (
                <th key={k} scope="col" className="min-w-[5.5rem] bg-surface-2 px-2 py-2.5 text-center text-xs font-semibold text-fg">
                  {networkLabel(k)}
                </th>
              ))}
              <th scope="col" className="min-w-[5.5rem] bg-surface-2 px-2 py-2.5 text-center text-xs font-semibold text-fg-muted">
                سایر شبکه‌ها
              </th>
            </tr>
          </thead>
          <tbody>
            {summary.locations.map((loc) => {
              const { cells, others } = locationRow(loc, keys);
              const info = locationClassInfo(loc.class);
              const newest = loc.networks.reduce<string | null>((a, n) => (a === null || n.at > a ? n.at : a), null);
              return (
                <tr key={loc.location}>
                  <th
                    scope="row"
                    className="sticky start-0 z-10 min-w-[7rem] border-t border-line bg-surface px-3 py-2 text-start align-middle font-normal"
                    title={newest ? `آخرین سنجش ${formatRelative(newest)}` : "سنجیده نشده"}
                  >
                    <div className="ltr text-right font-mono text-[0.85rem] font-bold text-fg">{loc.location}</div>
                    <Badge tone={info.tone} className="mt-1 px-2 py-0.5 text-[0.68rem]">
                      <span title={info.hint}>{info.label}</span>
                    </Badge>
                    {!loc.allowed ? <div className="mt-0.5 text-[0.66rem] whitespace-nowrap text-fg-faint">خارج از فهرست مجاز</div> : null}
                  </th>
                  {[...cells, others].map((c, i) => (
                    <td key={i} className="border-t border-line p-1.5">
                      <div className={cn("flex h-10 items-center justify-center rounded-xl text-xs font-bold", TONE_SOFT[c.tone])} title={c.title}>
                        {c.text}
                      </div>
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="px-1 text-xs leading-6 text-fg-muted">
        باز یعنی دست‌دادن TLS با دست‌کم یک node ما در آن location از آن اپراتور کار کرده است؛ بسته یعنی با همه‌شان ناموفق بوده، در حالی که سایت کنترل از همان شبکه باز بود. فقط {formatNumber(summary.window_days)} روز اخیر حساب میشود. ناوگان سرور تازه را اول در locationهای «باز» میسازد و در «بسته»ها هرگز.
      </p>
    </div>
  );
}
