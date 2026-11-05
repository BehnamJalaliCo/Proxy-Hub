import { useQueryClient } from "@tanstack/react-query";
import { Activity, CircleCheck, Globe, Hourglass, Megaphone, Network, RotateCcw, Save, Send, ShieldCheck, SlidersHorizontal, TriangleAlert } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type ReactNode, type SubmitEvent } from "react";

import { Button } from "../components/ui/Button";
import { Badge, Card, CardHeader } from "../components/ui/Card";
import { EndedSection } from "../components/ui/EndedSection";
import { CopyButton, EmptyState, LoadError, Skeleton } from "../components/ui/Feedback";
import { Field, TextInput, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorList, errorMessage, isApiError } from "../lib/api";
import { cn } from "../lib/browser";
import { isEndedProxy, splitEnded } from "../lib/ended";
import { formatJalali, formatNumber, formatRelative, toFaDigits } from "../lib/format";
import { nodeStateInfo } from "../lib/labels";
import {
  draftFromMtproto,
  endpointText,
  formatBytes,
  linkChanges,
  MODE_LABEL,
  mtprotoFromDraft,
  mtprotoKey,
  mtprotoStatusInfo,
  putMtprotoSettings,
  serverErrors,
  summarize,
  useMtproto,
  type MtprotoDraft,
  type MtprotoDraftErrors,
  type MtprotoProxy,
  type MtprotoSettings,
  type MtprotoState,
} from "../lib/mtproto";
import { toast } from "../lib/toast";
import { TONE_SOFT } from "../lib/tones";

const fadeUp = (i: number) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { delay: 0.04 * i, duration: 0.45, ease: [0.16, 1, 0.3, 1] as const },
});

const NOT_YET = {
  title: "پروکسی‌های MTProto هنوز روی پنل فعال نشده",
  description: "این بخش با به‌روزرسانی بعدی پنل فعال میشود؛ کاری از شما لازم نیست.",
};

function Tile({ icon, label, value, sub, index }: { icon: ReactNode; label: string; value: string; sub?: string; index: number }) {
  return (
    <motion.div {...fadeUp(index)}>
      <Card className="h-full p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[0.8rem] font-semibold text-fg-muted">{label}</span>
          <span className="grid size-8 place-items-center rounded-xl bg-brand-soft text-brand">{icon}</span>
        </div>
        <div className="num mt-2 text-[1.8rem] leading-none font-extrabold text-fg">{value}</div>
        {sub ? <div className="mt-1.5 text-xs text-fg-muted">{sub}</div> : null}
      </Card>
    </motion.div>
  );
}

function Fact({ label, children, latin = false }: { label: string; children: ReactNode; latin?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[0.72rem] text-fg-faint">{label}</dt>
      <dd className={cn("truncate text-sm font-semibold text-fg", latin && "ltr text-right")}>{children}</dd>
    </div>
  );
}

// --- one proxy ------------------------------------------------------------------------------------------

function ProxyCard({ p, index }: { p: MtprotoProxy; index: number }) {
  const st = mtprotoStatusInfo(p.status);
  const node = nodeStateInfo(p.node_state);
  const usable = p.status === "active" && p.link !== null;
  return (
    <motion.li {...fadeUp(index)} layout>
      <Card className={cn("flex h-full flex-col gap-4 p-5", (p.status === "expired" || p.status === "burned") && "opacity-75", p.status === "burned" && "border-danger/30")}>
        <div className="flex items-start gap-3">
          <span className={cn("grid size-11 shrink-0 place-items-center rounded-2xl", TONE_SOFT[st.tone])}>
            <Send className="size-5" />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[0.98rem] font-bold text-fg">{p.label}</h3>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {p.is_test ? <Badge tone="brand">آزمایشی</Badge> : null}
              <span className="ltr truncate font-mono text-xs text-fg-muted" title={p.node_name}>
                {p.node_name}
              </span>
            </div>
          </div>
          <Badge tone={st.tone} dot pulse={p.status === "pending"}>
            {st.label}
          </Badge>
        </div>
        <p className="-mt-1 text-[0.82rem] leading-6 text-fg-muted">{st.hint}</p>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          <div className="col-span-2">
            <Fact label={p.host_kind === "domain" ? "نشانی (نام جایگاه)" : "نشانی (IP خود node)"} latin>
              {endpointText(p)}
            </Fact>
          </div>
          <Fact label="نوع لینک">{MODE_LABEL[p.mode].label}</Fact>
          <Fact label="node">
            <span className="inline-flex items-center gap-1">
              <span className={cn("inline-block size-2 rounded-full", node.tone === "success" ? "bg-success" : node.tone === "danger" ? "bg-danger" : "bg-fg-faint")} aria-hidden />
              {node.label}
            </span>
          </Fact>
          <Fact label="فعال از">{p.live_at ? <span title={formatJalali(p.live_at, "datetime")}>{formatRelative(p.live_at)}</span> : "—"}</Fact>
          <Fact label="اسپانسر (tag)">{p.sponsor_tag_set ? "گذاشته شده" : "هنوز نه"}</Fact>
        </dl>

        <div className="grid grid-cols-2 gap-2 text-center">
          <div className="rounded-xl bg-surface-2 px-1 py-2">
            <div className="text-[0.68rem] text-fg-muted">اتصال الان</div>
            <div className="num text-lg font-extrabold text-fg">{p.connections === null ? "—" : formatNumber(p.connections)}</div>
          </div>
          <div className="rounded-xl bg-surface-2 px-1 py-2">
            <div className="text-[0.68rem] text-fg-muted">ترافیک کل</div>
            <div className="num ltr text-center text-sm font-bold text-fg">{toFaDigits(formatBytes(p.total_octets)).replace(".", "٫")}</div>
          </div>
        </div>

        <div className="mt-auto flex flex-wrap gap-2 border-t border-line pt-4">
          {p.link !== null && p.status !== "pending" ? <CopyButton text={p.link} label="کپی لینک" doneLabel="لینک کپی شد" /> : null}
          {usable && p.link ? (
            <a
              href={p.link}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex h-9 items-center gap-1.5 rounded-xl px-3 text-sm font-semibold text-fg-muted transition-colors hover:bg-surface-3/70 hover:text-fg"
            >
              <Send className="size-4" />
              باز کردن در تلگرام
            </a>
          ) : null}
          {p.status === "pending" ? <span className="text-xs leading-9 text-fg-faint">لینک پس از اجرای روی node آماده میشود.</span> : null}
        </div>
      </Card>
    </motion.li>
  );
}

// --- settings -----------------------------------------------------------------------------------------

function SettingsForm({ current, defaults, corrupt }: { current: MtprotoSettings; defaults: MtprotoSettings; corrupt: boolean }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<MtprotoDraft>(() => draftFromMtproto(current));
  const [server, setServer] = useState<MtprotoDraftErrors>({});
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const { value, errors } = mtprotoFromDraft(draft);
  const initial = draftFromMtproto(current);
  const dirty = corrupt || JSON.stringify(draft) !== JSON.stringify(initial);
  const changes = value ? linkChanges(current, value) : [];
  const shown = (k: keyof MtprotoDraftErrors): string | undefined => server[k] ?? (submitted ? errors[k] : undefined);

  const set = <K extends keyof MtprotoDraft>(k: K, v: MtprotoDraft[K]) => {
    setDraft((d) => ({ ...d, [k]: v }));
    setServer({});
  };

  const save = (e?: SubmitEvent) => {
    e?.preventDefault();
    setSubmitted(true);
    if (!value || busy) return;
    setBusy(true);
    putMtprotoSettings(value)
      .then(({ settings }) => {
        qc.setQueryData<MtprotoState>(mtprotoKey, (old) => (old ? { ...old, settings, settings_corrupt: false } : old));
        void qc.invalidateQueries({ queryKey: mtprotoKey });
        void qc.invalidateQueries({ queryKey: ["events"] });
        setDraft(draftFromMtproto(settings));
        setSubmitted(false);
        toast.success("تنظیمات MTProto ذخیره شد", "nodeها تا چند ثانیه‌ی دیگر تنظیم تازه را میگیرند.");
      })
      .catch((err: unknown) => {
        if (isApiError(err) && err.code === "invalid_settings") setServer(serverErrors(errorList(err, "fields")));
        toast.error("تنظیمات ذخیره نشد", errorMessage(err));
      })
      .finally(() => setBusy(false));
  };

  return (
    <Card>
      <CardHeader
        icon={<SlidersHorizontal className="size-5" />}
        title="تنظیمات MTProto"
        subtitle="برای همه‌ی nodeها. هر node یک secret ماندگار دارد؛ این‌ها فقط شکل لینک و پورت را تعیین میکنند."
      />
      <form onSubmit={save} noValidate className="flex flex-col gap-5 p-5">
        {corrupt ? (
          <div role="alert" className="flex gap-2 rounded-2xl bg-danger-soft p-3 text-sm leading-7 text-danger">
            <TriangleAlert className="mt-1 size-4 shrink-0" />
            تنظیمات ذخیره‌شده خراب است و پنل تا وقتی درستش نکنید هیچ node تازه‌ای را تنظیم نمیکند. مقدارها را بررسی کنید و ذخیره بزنید.
          </div>
        ) : null}
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="پورت" htmlFor="mtp-port" hint="پیش‌فرض ۸۴۴۳. پورت ۴۴۳ مال VLESS (WebSocket + TLS یا Reality) و ۲۰۸۳ مال XHTTP است. پورت دیگر فقط وقتی کار میکند که فایروال nodeها هم بازش کند." error={shown("port")}>
            <TextInput id="mtp-port" inputMode="numeric" latin value={draft.port} onChange={(e) => set("port", e.target.value)} invalid={Boolean(shown("port"))} />
          </Field>
          <Field
            label="دامنه‌ی TLS پشتیبان (برای لینک ee)"
            htmlFor="mtp-domain"
            hint="nodeی که نام دامنه‌ی خودش و گواهی دارد (روش WebSocket + TLS)، نام خودش را به‌جای این میگذارد تا نام و IP یکی باشند. این دامنه فقط برای nodeهای بی‌نام است: سایت بزرگ و پایداری که در ایران فیلتر نیست."
            error={shown("tls_domain")}
          >
            <TextInput id="mtp-domain" latin value={draft.tls_domain} onChange={(e) => set("tls_domain", e.target.value)} invalid={Boolean(shown("tls_domain"))} placeholder="www.microsoft.com" />
          </Field>
        </div>
        <div className="flex flex-col gap-4 rounded-2xl border border-line bg-surface-2/60 p-4">
          <Toggle checked={draft.mode_tls} onCheckedChange={(v) => set("mode_tls", v)} label={MODE_LABEL.tls.label} description={MODE_LABEL.tls.hint} />
          <Toggle checked={draft.mode_secure} onCheckedChange={(v) => set("mode_secure", v)} label={MODE_LABEL.secure.label} description={MODE_LABEL.secure.hint} />
          <Toggle checked={draft.mode_classic} onCheckedChange={(v) => set("mode_classic", v)} label={MODE_LABEL.classic.label} description={MODE_LABEL.classic.hint} />
          <p className="text-[0.8rem] leading-6 text-fg-muted">لینک هر پروکسی از اولین حالت روشن ساخته میشود (TLS، بعد dd، بعد ساده).</p>
          {shown("modes") ? (
            <p role="alert" className="text-[0.82rem] leading-6 text-danger">
              {shown("modes")}
            </p>
          ) : null}
        </div>
        <AnimatePresence initial={false}>
          {changes.length > 0 ? (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div role="status" className="flex gap-2 rounded-2xl bg-warning-soft p-3 text-sm leading-7 text-warning">
                <TriangleAlert className="mt-1 size-4 shrink-0" />
                با این تغییر ({changes.join("، ")}) لینک همه‌ی پروکسی‌ها عوض میشود و لینک‌هایی که پیش‌تر در کانال گذاشته شده از کار میافتند.
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" icon={<Save className="size-4" />} loading={busy} disabled={!dirty}>
            ذخیره
          </Button>
          <Button variant="ghost" icon={<RotateCcw className="size-4" />} onClick={() => setDraft(draftFromMtproto(defaults))}>
            مقدارهای پیش‌فرض
          </Button>
        </div>
      </form>
    </Card>
  );
}

// --- page ----------------------------------------------------------------------------------------------

export default function MtprotoPage() {
  const q = useMtproto();
  const data = q.data;
  const s = data ? summarize(data.proxies) : null;
  const split = data ? splitEnded(data.proxies, isEndedProxy) : null;

  return (
    <>
      <PageHeader
        title="پروکسی‌های MTProto"
        subtitle="روی هر node یک پروکسی Telemt با secret ماندگار. لینک‌ها همان‌اند که در کانال منتشر میشوند. فقط تعداد اتصال‌ها، هیچ IP کاربری."
      />
      <div className="flex flex-col gap-4 sm:gap-5">
        {q.isPending ? (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-28 rounded-3xl" />
              ))}
            </div>
            <Skeleton className="h-80 rounded-3xl" />
          </>
        ) : q.isError ? (
          <Card>
            <LoadError error={q.error} notYet={NOT_YET} onRetry={() => void q.refetch()} />
          </Card>
        ) : data && s ? (
          <>
            <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
              <Tile index={0} icon={<CircleCheck className="size-4" />} label="پروکسی فعال" value={formatNumber(s.active)} sub="روی node اجرا میشود" />
              <Tile index={1} icon={<Activity className="size-4" />} label="اتصال الان" value={s.connections === null ? "—" : formatNumber(s.connections)} sub="جمع پروکسی‌های فعال (فقط تعداد)" />
              <Tile index={2} icon={<Hourglass className="size-4" />} label="در حال راه‌اندازی" value={formatNumber(s.pending)} sub="منتظر گزارش عامل node" />
              <Tile index={3} icon={<Megaphone className="size-4" />} label="با tag اسپانسر" value={formatNumber(s.sponsored)} sub="از پروکسی‌های فعال" />
            </div>

            <div className={cn("flex items-start gap-2 rounded-2xl p-3 text-sm leading-7", data.active_domain ? "bg-success-soft text-success" : "bg-info-soft text-info")}>
              {data.active_domain ? <Globe className="mt-1 size-4 shrink-0" /> : <Network className="mt-1 size-4 shrink-0" />}
              {data.active_domain ? (
                <span>
                  نشانی لینک‌ها نام جایگاه روی دامنه‌ی فعال است، مثل <bdi className="ltr inline-block font-mono">slot1.{data.active_domain}</bdi>، وقتی رکوردش در Cloudflare به همان node اشاره کند؛ وگرنه IP خود node.
                </span>
              ) : (
                <span>دامنه‌ی فعالی نیست؛ لینک‌ها با IP خود nodeها ساخته میشوند و با آمدن دامنه خودکار به نام جایگاه عوض میشوند.</span>
              )}
            </div>

            {split && split.current.length === 0 ? (
              <Card>
                <EmptyState
                  icon={<ShieldCheck className="size-6" />}
                  title={split.ended.length ? "پروکسی فعالی نیست" : "هنوز پروکسی‌ای نیست"}
                  description="وقتی nodeی ساخته شود، پنل خودش Telemt و secret آن را تنظیم میکند و لینکش اینجا میآید."
                />
              </Card>
            ) : split ? (
              <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {split.current.map((p, i) => (
                  <ProxyCard key={p.key} p={p} index={i} />
                ))}
              </ul>
            ) : null}
            {split ? (
              <EndedSection count={split.ended.length} hint="nodeهای کنارگذاشته یا فیلترشده؛ لینکشان دیگر منتشر نمیشود.">
                <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {split.ended.map((p, i) => (
                    <ProxyCard key={p.key} p={p} index={i} />
                  ))}
                </ul>
              </EndedSection>
            ) : null}

            <SettingsForm
              key={JSON.stringify(data.settings ?? data.defaults)}
              current={data.settings ?? data.defaults}
              defaults={data.defaults}
              corrupt={data.settings_corrupt}
            />
            <p className="text-center text-xs text-fg-faint">به‌روزرسانی: {formatJalali(new Date(q.dataUpdatedAt), "time")}</p>
          </>
        ) : null}
      </div>
    </>
  );
}
