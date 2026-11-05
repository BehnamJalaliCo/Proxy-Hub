import { useQueryClient } from "@tanstack/react-query";
import { Crosshair, Save, Undo2 } from "lucide-react";
import { useState } from "react";

import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import * as api from "../lib/endpoints";
import { formatJalali, formatNumber, formatRelative } from "../lib/format";
import { CURRENT_FOR, candidateState, candidatesProblem, networkLabel, nodeCheckInfo, normalizeHost, targetCell } from "../lib/iranReach";
import { keys, useTargets } from "../lib/queries";
import { toast } from "../lib/toast";
import { TONE_SOFT } from "../lib/tones";
import type { TargetsSummary } from "../lib/types";
import { Button } from "./ui/Button";
import { Badge, Card, CardHeader } from "./ui/Card";
import { LoadError, SkeletonLines } from "./ui/Feedback";
import { Field, TagInput } from "./ui/Form";

const SUGGESTIONS = ["github.com", "www.speedtest.net", "aws.amazon.com", "www.cloudflare.com", "www.mozilla.org", "addons.mozilla.org", "www.samsung.com"];

function CandidatesEditor({ data }: { data: TargetsSummary }) {
  const qc = useQueryClient();
  const [hosts, setHosts] = useState<string[]>(data.settings.candidates);
  const [busy, setBusy] = useState(false);
  const problem = candidatesProblem(hosts);
  const dirty = JSON.stringify(hosts) !== JSON.stringify(data.settings.candidates);

  const save = () => {
    if (problem || busy) return;
    setBusy(true);
    api
      .putTargetSettings({ ...data.settings, candidates: hosts })
      .then((saved) => {
        qc.setQueryData(keys.targets, saved);
        void qc.invalidateQueries({ queryKey: ["events"] });
        setHosts(saved.settings.candidates);
        toast.success("فهرست سایت‌های نامزد ذخیره شد", "سایت‌های تازه چند دقیقه‌ی دیگر از ایران سنجیده میشوند.");
      })
      .catch((err: unknown) => toast.error("فهرست ذخیره نشد", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="flex flex-col gap-3">
      <Field
        label="سایت‌های نامزد"
        htmlFor="t-candidates"
        hint="سایت‌های بزرگ خارجی با TLS 1.3 و HTTP/2؛ Enter برای افزودن. پنل هدف Reality و دامنه‌ی TLS پروکسی‌های MTProto را در حالت خودکار از میان همین‌ها برمیگزیند."
        error={dirty ? (problem ?? undefined) : undefined}
      >
        <TagInput id="t-candidates" values={hosts} onChange={setHosts} suggestions={SUGGESTIONS} normalize={normalizeHost} placeholder="github.com" invalid={dirty && problem !== null} />
      </Field>
      {dirty ? (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" loading={busy} disabled={problem !== null} icon={<Save className="size-4" />} onClick={save}>
            ذخیره‌ی فهرست
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} icon={<Undo2 className="size-4" />} onClick={() => setHosts(data.settings.candidates)}>
            برگرداندن
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function CandidatesTable({ data }: { data: TargetsSummary }) {
  const keyNets = data.key_networks;
  return (
    <div className="overflow-x-auto rounded-2xl border border-line">
      <table className="w-full border-separate border-spacing-0 text-sm">
        <caption className="sr-only">باز بودن هر سایت نامزد از شبکه‌های ایران، با دست‌دادن TLS</caption>
        <thead>
          <tr>
            <th scope="col" className="sticky start-0 z-10 bg-surface-2 px-3 py-2.5 text-start text-xs font-semibold text-fg-muted">
              سایت
            </th>
            {keyNets.map((k) => (
              <th key={k} scope="col" className="min-w-[5.5rem] bg-surface-2 px-2 py-2.5 text-center text-xs font-semibold text-fg">
                {networkLabel(k)}
              </th>
            ))}
            <th scope="col" className="bg-surface-2 px-2 py-2.5 text-center text-xs font-semibold text-fg-muted">
              آزمون nodeها
            </th>
          </tr>
        </thead>
        <tbody>
          {data.candidates.map((c) => {
            const state = candidateState(c);
            const check = nodeCheckInfo(c);
            return (
              <tr key={c.host}>
                <th scope="row" className="sticky start-0 z-10 min-w-[10rem] border-t border-line bg-surface px-3 py-2 text-start align-middle font-normal">
                  <div className="ltr max-w-[12rem] truncate text-right font-mono text-[0.8rem] font-bold text-fg" title={c.host}>
                    {c.host}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    <Badge tone={state.tone} className="px-2 py-0.5 text-[0.66rem]">
                      {state.label}
                    </Badge>
                    {c.current_for.map((k) => (
                      <Badge key={k} tone="brand" className="px-2 py-0.5 text-[0.66rem]">
                        {CURRENT_FOR[k] ?? k}
                      </Badge>
                    ))}
                    {!c.listed ? <span className="text-[0.68rem] text-fg-faint">خارج از فهرست</span> : null}
                    {c.measured_at ? <span className="text-[0.68rem] text-fg-faint">{formatRelative(c.measured_at)}</span> : null}
                  </div>
                </th>
                {keyNets.map((k) => {
                  const cell = targetCell(c.networks.find((n) => n.key === k));
                  return (
                    <td key={k} className="border-t border-line p-1.5">
                      <div className={cn("flex h-10 items-center justify-center rounded-xl text-xs font-bold", TONE_SOFT[cell.tone])}>
                        <span className={cell.text.endsWith("ms") ? "ltr" : undefined}>{cell.text}</span>
                      </div>
                    </td>
                  );
                })}
                <td className="border-t border-line px-2 py-1.5 text-center text-xs">
                  <span className={cn("rounded-lg px-2 py-1", TONE_SOFT[check.tone])}>{check.label}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Settings page: the candidate REALITY targets / fake-TLS domains and what Iran says about them. */
export function TargetCandidates() {
  const targets = useTargets();
  const data = targets.data;
  const mode = (m: "auto" | "manual") => (m === "auto" ? "خودکار" : "دستی");
  const subtitle = data
    ? `هدف Reality: ${data.reality.target} (${mode(data.reality.mode)})` +
      (data.mtproto ? ` · دامنه‌ی MTProto: ${data.mtproto.tls_domain} (${mode(data.mtproto.mode)})` : "")
    : "هر سایت نامزد هر چند ساعت با دست‌دادن TLS از همراه اول و رایتل سنجیده میشود.";
  return (
    <Card>
      <CardHeader icon={<Crosshair className="size-5" />} title="سایت‌های هدف (SNI)" subtitle={subtitle} />
      <div className="flex flex-col gap-4 p-5">
        {targets.isPending ? (
          <SkeletonLines lines={4} />
        ) : targets.isError ? (
          <LoadError
            error={targets.error}
            compact
            onRetry={() => void targets.refetch()}
            notYet={{
              title: "انتخاب خودکار سایت هدف هنوز روی پنل فعال نشده",
              description: "با به‌روزرسانی بعدی پنل، سایت‌های نامزد از ایران سنجیده میشوند.",
            }}
          />
        ) : data ? (
          <>
            <CandidatesEditor key={JSON.stringify(data.settings.candidates)} data={data} />
            <CandidatesTable data={data} />
            <p className="text-xs leading-6 text-fg-muted">
              {data.last_full_at ? `آخرین سنجش کامل ${formatJalali(data.last_full_at, "short")}` : "هنوز سنجش کاملی انجام نشده"}
              {data.next_full_at ? ` · بعدی حدود ${formatJalali(data.next_full_at, "time")}` : ""}
              {` · هر ${formatNumber(data.settings.interval_h)} ساعت. در حالت خودکار، هدف Reality بهترین سایت باز از ایران است که nodeها هم قبولش دارند؛ دامنه‌ی MTProto فقط وقتی عوض میشود که در دو سنجش پشت‌سرهم بسته باشد. هر تغییر در «رویدادها» ثبت و به شما خبر داده میشود.`}
            </p>
          </>
        ) : null}
      </div>
    </Card>
  );
}
