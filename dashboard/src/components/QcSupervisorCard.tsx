// The content supervisor (decision D52): what it is doing, and its settings. Everything the owner
// changes here is checked again by the server (panel/proxyhub/content/qc_settings.py).

import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, KeyRound, Save, ShieldCheck, Trash2 } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type ReactNode } from "react";

import { errorMessage } from "../lib/api";
import { cn } from "../lib/browser";
import {
  API_KEY_RE,
  API_MODEL_LABEL,
  BACKEND_LABEL,
  bridgeState,
  CATEGORY_LABEL,
  FLAG_LABEL,
  MODE_LABEL,
  parseCount,
  parseWords,
  QC_NUMBERS,
  removeApiKey,
  saveQc,
  setApiKey,
  type ApiModel,
  type QcBackend,
  type QcConfig,
  type QcMode,
  type QcNumberKey,
  type QcView,
} from "../lib/contentEngine";
import { contentKeys, useQc } from "../lib/contentEngineQueries";
import { formatNumber, formatRelative } from "../lib/format";
import { toast } from "../lib/toast";
import { Button } from "./ui/Button";
import { Badge, Card, CardHeader } from "./ui/Card";
import { LoadError, Skeleton } from "./ui/Feedback";
import { Field, SecretInput, Segmented, TextInput, Toggle } from "./ui/Form";

const NOT_YET = {
  title: "ناظر محتوا هنوز روی پنل فعال نشده",
  description: "با به‌روزرسانی بعدی پنل فعال میشود. کاری از شما لازم نیست.",
};

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-2xl bg-surface-2 p-3">
      <span className="text-[0.78rem] font-semibold text-fg-muted">{label}</span>
      <span className="text-sm leading-6 font-semibold text-fg">{children}</span>
    </div>
  );
}

function used(n: number, cap: number): string {
  return `${formatNumber(n)} از ${formatNumber(cap)}`;
}

function Status({ view }: { view: QcView }) {
  const bridge = bridgeState(view);
  const cfg = view.config;
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Stat label="حالت">
          <Badge tone={cfg.mode === "auto" ? "success" : "info"}>{MODE_LABEL[cfg.mode]}</Badge>
        </Stat>
        <Stat label="Claude">
          <Badge tone={bridge.tone} dot>
            {bridge.label}
          </Badge>
        </Stat>
        <Stat label="بررسی در ۲۴ ساعت">
          اشتراک {used(view.calls_24h.bridge, cfg.bridge_daily_cap)}
          <br />
          API {used(view.calls_24h.api, cfg.api_daily_cap)}
        </Stat>
        <Stat label="تصمیم‌های ۲۴ ساعت">
          {formatNumber(view.decisions_24h.approve)} تأیید، {formatNumber(view.decisions_24h.reject)} رد
          <br />
          آماده‌ی پست: {formatNumber(view.ready)}
        </Stat>
      </div>
      {view.unavailable_since ? (
        <p className="rounded-2xl bg-warning-soft p-3.5 text-sm leading-7 text-fg">
          Claude از {formatRelative(view.unavailable_since)} در دسترس نیست: {view.unavailable_reason_fa}
          {" "}پست‌ها بی‌وقفه با بانک محتوای همیشگی میروند و چیزی بی بررسی منتشر نمیشود.
        </p>
      ) : null}
      <p className="text-[0.82rem] leading-6 text-fg-faint">
        {view.last_outcome_fa ? `آخرین تماس (${formatRelative(view.last_outcome_at)}): ${view.last_outcome_fa}` : "هنوز تماسی با Claude نبوده است."}
        {view.bridge?.claude_version ? ` Claude Code روی سرور: ${view.bridge.claude_version}.` : ""}
        {view.bridge?.rate_limit?.utilization !== undefined
          ? ` مصرف اشتراک: ${formatNumber(Math.round(view.bridge.rate_limit.utilization * 100))}٪.`
          : ""}
      </p>
    </div>
  );
}

type Numbers = Record<QcNumberKey, string>;

function numbersOf(cfg: QcConfig): Numbers {
  return Object.fromEntries(QC_NUMBERS.map((f) => [f.key, String(cfg[f.key])])) as Numbers;
}

function Settings({ view }: { view: QcView }) {
  const qc = useQueryClient();
  const [cfg, setCfg] = useState<QcConfig>(view.config);
  const [numbers, setNumbers] = useState<Numbers>(numbersOf(view.config));
  const [words, setWords] = useState(view.config.forbidden_words.join("\n"));
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const parsed = QC_NUMBERS.map((f) => [f.key, parseCount(numbers[f.key], f.min, f.max)] as const);
  const errors = Object.fromEntries(parsed.filter(([, v]) => typeof v === "string")) as Partial<Record<QcNumberKey, string>>;
  const valid = Object.keys(errors).length === 0 && cfg.categories.length > 0;
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: contentKeys.all });
  };
  const act = (what: string, p: Promise<unknown>, done: string) => {
    setBusy(what);
    p.then(() => {
      toast.success(done);
      refresh();
    })
      .catch((err: unknown) => toast.error("انجام نشد", errorMessage(err)))
      .finally(() => setBusy(null));
  };
  const save = () => {
    const next: QcConfig = { ...cfg, forbidden_words: parseWords(words) };
    for (const [k, v] of parsed) if (typeof v === "number") next[k] = v;
    act("save", saveQc(next), "تنظیمات ناظر ذخیره شد");
  };
  const toggleCategory = (c: string) =>
    setCfg((x) => ({
      ...x,
      categories: x.categories.includes(c) ? x.categories.filter((y) => y !== c) : [...x.categories, c],
    }));
  return (
    <div className="flex flex-col gap-4">
      <Field
        label="حالت کار"
        htmlFor="qc-mode"
        hint={
          cfg.mode === "auto"
            ? "خبری که Claude تأیید کند و همه‌ی قاعده‌ها را بگذراند، بی تأیید شما میرود (D52)."
            : "Claude فقط پیشنهاد میدهد؛ هیچ خبری بی تأیید شما نمیرود."
        }
      >
        <Segmented<QcMode>
          value={cfg.mode}
          onChange={(mode) => setCfg((x) => ({ ...x, mode }))}
          label="حالت کار"
          options={(["auto", "review"] as const).map((m) => ({ value: m, label: MODE_LABEL[m] }))}
          className="self-start"
        />
      </Field>
      <Field label="راه رسیدن به Claude" htmlFor="qc-backend" hint="اشتراک Claude روی سرور اصلی است (D19)؛ کلید API فقط پشتیبان است.">
        <div className="-mx-1 overflow-x-auto px-1 pb-1">
          <Segmented<QcBackend>
            value={cfg.backend}
            onChange={(backend) => setCfg((x) => ({ ...x, backend }))}
            label="راه رسیدن به Claude"
            options={(["auto", "bridge", "api", "off"] as const).map((b) => ({ value: b, label: BACKEND_LABEL[b] }))}
            className="min-w-max"
          />
        </div>
      </Field>
      <Field label="مدل کلید API" htmlFor="qc-model">
        <Segmented<ApiModel>
          value={cfg.api_model}
          onChange={(api_model) => setCfg((x) => ({ ...x, api_model }))}
          label="مدل کلید API"
          options={view.options.api_models.map((m) => ({ value: m, label: API_MODEL_LABEL[m] }))}
          className="self-start"
        />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        {QC_NUMBERS.map((f) => (
          <Field key={f.key} label={f.label} htmlFor={`qc-${f.key}`} hint={f.hint || undefined} error={errors[f.key] ?? null}>
            <TextInput
              id={`qc-${f.key}`}
              inputMode="numeric"
              latin
              value={numbers[f.key]}
              onChange={(e) => setNumbers((x) => ({ ...x, [f.key]: e.target.value }))}
              invalid={Boolean(errors[f.key])}
            />
          </Field>
        ))}
      </div>
      <Field
        label="دسته‌های مجاز"
        htmlFor="qc-categories"
        hint="خبر، از جمله سیاسی، و سرگرمی همه مجازند؛ دسته‌ای را که نمیخواهید خاموش کنید."
        error={cfg.categories.length === 0 ? "دست‌کم یک دسته لازم است." : null}
      >
        <div id="qc-categories" className="flex flex-wrap gap-1.5">
          {view.options.categories.map((c) => {
            const on = cfg.categories.includes(c);
            return (
              <button
                key={c}
                type="button"
                aria-pressed={on}
                onClick={() => toggleCategory(c)}
                className={cn(
                  "h-8 rounded-lg border px-3 text-[0.82rem] font-semibold transition-colors",
                  on ? "border-brand bg-brand-soft text-brand" : "border-dashed border-line-strong text-fg-muted hover:text-fg",
                )}
              >
                {CATEGORY_LABEL[c] ?? c}
              </button>
            );
          })}
        </div>
      </Field>
      <div className="flex flex-col gap-3 rounded-2xl bg-surface-2 p-3.5">
        <p className="text-sm font-semibold text-fg">رد خودکار وقتی Claude این‌ها را ببیند</p>
        {view.options.flags.map((flag) => (
          <Toggle
            key={flag}
            checked={cfg.block[flag]}
            onCheckedChange={(v) => setCfg((x) => ({ ...x, block: { ...x.block, [flag]: v } }))}
            label={FLAG_LABEL[flag] ?? flag}
          />
        ))}
        <p className="text-[0.82rem] leading-6 text-fg-faint">
          «{FLAG_LABEL["manipulation"]}» همیشه رد میشود. لینک، @، هشتگ، شماره‌ی تلفن، نام منبع و امضا را کد هم جدا رد میکند.
        </p>
      </div>
      <Field label="واژه‌های ممنوع" htmlFor="qc-words" hint="هر واژه در یک خط. کپشنی که یکی از آن‌ها را داشته باشد رد میشود.">
        <textarea
          id="qc-words"
          value={words}
          onChange={(e) => setWords(e.target.value)}
          rows={3}
          className="w-full rounded-xl border border-line bg-surface-2 p-3 text-[0.95rem] leading-7 text-fg outline-none focus:border-brand focus:bg-surface focus:ring-4 focus:ring-[var(--ph-ring)]"
        />
      </Field>
      <Button
        variant="primary"
        icon={<Save className="size-4" />}
        loading={busy === "save"}
        disabled={!valid || busy !== null}
        onClick={save}
        className="self-start"
      >
        ذخیره‌ی تنظیمات ناظر
      </Button>
      <div className="flex flex-col gap-2 border-t border-line pt-4">
        <Field
          label="کلید API Anthropic (پشتیبان)"
          htmlFor="qc-key"
          hint={
            view.api_key_set
              ? "یک کلید ذخیره شده است و دیگر نمایش داده نمیشود. برای عوض کردن، کلید تازه را بنویسید."
              : "اختیاری. فقط وقتی اشتراک روی سرور در دسترس نیست به کار میرود."
          }
          error={key && !API_KEY_RE.test(key.trim()) ? "کلید باید با sk-ant- شروع شود." : null}
        >
          <SecretInput id="qc-key" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" placeholder="sk-ant-…" />
        </Field>
        <div className="flex flex-wrap gap-1.5">
          <Button
            variant="secondary"
            size="sm"
            icon={<KeyRound className="size-4" />}
            loading={busy === "key"}
            disabled={!API_KEY_RE.test(key.trim()) || busy !== null}
            onClick={() => {
              act("key", setApiKey(key.trim()), "کلید در گاوصندوق پنل ذخیره شد");
              setKey("");
            }}
          >
            ذخیره‌ی کلید
          </Button>
          {view.api_key_set ? (
            <Button
              variant="ghost"
              size="sm"
              icon={<Trash2 className="size-4" />}
              loading={busy === "remove"}
              disabled={busy !== null}
              onClick={() => act("remove", removeApiKey(), "کلید پاک شد")}
            >
              پاک کردن کلید
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function QcSupervisorCard() {
  const query = useQc();
  const [open, setOpen] = useState(false);
  const view = query.data;
  return (
    <Card>
      <CardHeader
        icon={<ShieldCheck className="size-5" />}
        title="ناظر محتوا (Claude)"
        subtitle="هر خبر پیش از رفتن به کانال با Claude و قاعده‌های قطعی بررسی میشود. اگر چیزی آماده نباشد، پست با بانک محتوا میرود؛ پستی جا نمیافتد."
      />
      <div className="flex flex-col gap-3 p-4 sm:p-5">
        {query.isPending ? (
          <Skeleton className="h-28 rounded-2xl" />
        ) : query.isError ? (
          <LoadError error={query.error} onRetry={() => void query.refetch()} notYet={NOT_YET} />
        ) : view ? (
          <>
            <Status view={view} />
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="inline-flex items-center gap-1 self-start text-sm font-semibold text-brand hover:underline"
              aria-expanded={open}
            >
              <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
              تنظیمات ناظر
            </button>
            <AnimatePresence initial={false}>
              {open ? (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  className="overflow-hidden"
                >
                  <Settings key={JSON.stringify(view.config) + String(view.api_key_set)} view={view} />
                </motion.div>
              ) : null}
            </AnimatePresence>
          </>
        ) : null}
      </div>
    </Card>
  );
}
