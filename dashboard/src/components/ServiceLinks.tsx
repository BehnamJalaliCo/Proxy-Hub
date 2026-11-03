import { ArrowLeft, Cloud, Send, ServerCog } from "lucide-react";
import { motion } from "motion/react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { isEndpointMissing } from "../lib/api";
import { activeZone } from "../lib/cloudflare";
import { formatNumber, toFaDigits } from "../lib/format";
import type { Tone } from "../lib/labels";
import { isLiveNode, liveTestNode } from "../lib/nodes";
import { useCloudflareStatus, useNodes, useTelegramStatus } from "../lib/queries";
import { TONE_SOFT } from "../lib/tones";
import { StatusDot } from "./ui/Card";
import { Skeleton } from "./ui/Feedback";

interface Line {
  tone: Tone;
  text: string;
  sub: string;
}

/** A query's state as one line: loading (null), not on this panel yet, an error, or `ok(data)`. */
function lineOf<T>(q: { data: T | undefined; isPending: boolean; error: unknown }, ok: (data: T) => Line): Line | null {
  if (q.data !== undefined) return ok(q.data);
  if (q.isPending) return null;
  if (isEndpointMissing(q.error)) return { tone: "neutral", text: "به‌زودی", sub: "با به‌روزرسانی بعدی پنل فعال میشود" };
  return { tone: "danger", text: "بارگذاری نشد", sub: "برای جزئیات صفحه را باز کنید" };
}

function LinkCard({ to, icon, title, line, index }: { to: string; icon: ReactNode; title: string; line: Line | null; index: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.04 * index, duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
    >
      <Link
        to={to}
        className="group flex h-full items-center gap-3 rounded-3xl border border-line bg-surface p-4 shadow-card transition-transform duration-300 hover:-translate-y-0.5"
      >
        <span className={`grid size-11 shrink-0 place-items-center rounded-2xl ${TONE_SOFT[line?.tone ?? "neutral"]}`}>{icon}</span>
        <span className="min-w-0 flex-1">
          <span className="block text-[0.8rem] font-semibold text-fg-muted">{title}</span>
          {line ? (
            <>
              <span className="flex items-center gap-1.5 text-base font-extrabold text-fg">
                <StatusDot tone={line.tone} />
                <span className="truncate">{line.text}</span>
              </span>
              <span className="block truncate text-xs text-fg-muted">{line.sub}</span>
            </>
          ) : (
            <Skeleton className="mt-1 h-9 w-full" />
          )}
        </span>
        <ArrowLeft className="size-5 shrink-0 text-fg-faint transition-transform group-hover:-translate-x-0.5" />
      </Link>
    </motion.div>
  );
}

/** Small status cards on the overview: nodes, Telegram and the domain pool, each a link. */
export function ServiceLinks() {
  const nodes = useNodes();
  const tg = useTelegramStatus();
  const cf = useCloudflareStatus();

  const nodeLine = lineOf(nodes, (list) => {
    const live = list.filter(isLiveNode);
    const ready = live.filter((n) => n.state === "ready").length;
    const bad = live.filter((n) => n.state === "unhealthy" || n.state === "burned").length;
    return {
      tone: bad > 0 ? "danger" : live.length === 0 ? "neutral" : "success",
      text: `${formatNumber(live.length)} زنده · ${formatNumber(ready)} آماده`,
      sub: bad > 0 ? `${formatNumber(bad)} ناسالم یا سوخته` : liveTestNode(list) ? "یک node آزمایشی در کار است" : "بی node آزمایشی",
    };
  });

  const tgLine = lineOf(tg, (s) => {
    const done = [s.bot !== null, s.owner_paired, s.channels.test !== null, s.channels.main !== null].filter(Boolean).length;
    const deleteRight = s.channels.main?.can_delete === true || s.channels.test?.can_delete === true;
    return {
      tone: deleteRight || s.outbox.failed > 0 ? "danger" : done === 4 ? "success" : "warning",
      text: done === 4 ? "آماده" : `${toFaDigits(done)} از ۴ گام راه‌اندازی`,
      sub: deleteRight
        ? "ربات اجازه‌ی حذف پیام دارد؛ بردارید"
        : s.outbox.failed > 0
          ? `${formatNumber(s.outbox.failed)} پیام ناموفق در صف`
          : s.bot
            ? `‎@${s.bot.username}‎ · ${s.owner_paired ? "چت شما وصل" : "چت شما وصل نیست"}`
            : "توکن ربات وارد نشده",
    };
  });

  const cfLine = lineOf(cf, (s) => {
    const active = activeZone(s.zones);
    if (!s.token_set) return { tone: "warning", text: "توکن وارد نشده", sub: "برای نام‌های پروکسی لازم است" };
    return {
      tone: !s.verified ? "danger" : active ? "success" : "warning",
      text: active ? active.name : "دامنه‌ی فعال ندارد",
      sub: `${formatNumber(s.zones.length)} دامنه · ${formatNumber(s.records.length)} رکورد`,
    };
  });

  return (
    <div className="grid gap-3 sm:grid-cols-3 sm:gap-4">
      <LinkCard index={0} to="/nodes" icon={<ServerCog className="size-5" />} title="nodeها" line={nodeLine} />
      <LinkCard index={1} to="/telegram" icon={<Send className="size-5 rtl:-scale-x-100" />} title="تلگرام" line={tgLine} />
      <LinkCard index={2} to="/cloudflare" icon={<Cloud className="size-5" />} title="Cloudflare و دامنه‌ها" line={cfLine} />
    </div>
  );
}
