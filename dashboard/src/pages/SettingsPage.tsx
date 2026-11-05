import { useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Gauge, MapPin, Save, Server, SlidersHorizontal, Undo2 } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState, type SubmitEvent, type ReactNode } from "react";

import { KillSwitchControl } from "../components/KillSwitchControl";
import { TargetCandidates } from "../components/TargetCandidates";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { ErrorState, Skeleton } from "../components/ui/Feedback";
import { Field, TagInput, TextInput, Toggle } from "../components/ui/Form";
import { PageHeader } from "../components/ui/PageHeader";
import { errorMessage, isApiError } from "../lib/api";
import * as api from "../lib/endpoints";
import { formatJalali } from "../lib/format";
import { SETTINGS_LABEL } from "../lib/labels";
import { keys, useFleetSettings } from "../lib/queries";
import { toast } from "../lib/toast";
import type { FleetSettings } from "../lib/types";
import {
  draftFromSettings,
  sameSettings,
  settingsFromDraft,
  type DraftErrors,
  type SettingsDraft,
} from "../lib/validation";

const LOCATIONS = ["fsn1", "nbg1", "hel1", "ash", "hil"];
const SERVER_TYPES = ["cpx02", "cx23", "cpx12"];

/** 422 {"fields":[{"loc":[...,"<field>"],"type":...}]} -> which form fields the server rejected. */
function serverFieldErrors(err: unknown): DraftErrors {
  const out: DraftErrors = {};
  if (!isApiError(err)) return out;
  if (err.code === "live_mode_locked") {
    out.dry_run = err.messageFa;
    return out;
  }
  const fields = err.body["fields"];
  if (!Array.isArray(fields)) return out;
  const known = new Set<string>(Object.keys(SETTINGS_LABEL));
  for (const f of fields as unknown[]) {
    if (typeof f !== "object" || f === null) continue;
    const loc = (f as { loc?: unknown }).loc;
    if (!Array.isArray(loc)) continue;
    const key = (loc as unknown[]).map(String).find((p) => known.has(p));
    if (key && key !== "kill_switch") out[key as keyof SettingsDraft] = "سرور این مقدار را نپذیرفت؛ محدوده‌ی مجاز را بررسی کنید.";
  }
  return out;
}

function Section({ icon, title, subtitle, children }: { icon: ReactNode; title: string; subtitle: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader icon={icon} title={title} subtitle={subtitle} />
      <div className="grid gap-5 p-5 sm:grid-cols-2">{children}</div>
    </Card>
  );
}

function SettingsForm({ initial, onSaved }: { initial: FleetSettings; onSaved: (s: FleetSettings) => void }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFromSettings(initial));
  const [serverErrors, setServerErrors] = useState<DraftErrors>({});
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);

  const { settings, errors } = settingsFromDraft(draft, initial);
  const dirty = !sameSettings(settings, initial) || JSON.stringify(draft) !== JSON.stringify(draftFromSettings(initial));
  const hasErrors = Object.keys(errors).length > 0;
  const shown = (k: keyof SettingsDraft): string | undefined => serverErrors[k] ?? (submitted || draft[k] !== draftFromSettings(initial)[k] ? errors[k] : undefined);

  const set = <K extends keyof SettingsDraft>(k: K, v: SettingsDraft[K]) => {
    setDraft((d) => ({ ...d, [k]: v }));
    setServerErrors((e) => Object.fromEntries(Object.entries(e).filter(([key]) => key !== k)));
  };

  const save = (e?: SubmitEvent) => {
    e?.preventDefault();
    setSubmitted(true);
    if (hasErrors || busy) return;
    setBusy(true);
    // Re-read the kill switch right before saving, so a stale form can never undo an emergency stop.
    api
      .getFleetSettings()
      .then((fresh) => api.putFleetSettings({ ...settings, kill_switch: fresh.kill_switch }))
      .then((saved) => {
        qc.setQueryData(keys.settings, saved);
        void qc.invalidateQueries({ queryKey: keys.overview });
        void qc.invalidateQueries({ queryKey: keys.plan });
        void qc.invalidateQueries({ queryKey: ["events"] });
        toast.success("تنظیمات ذخیره شد", `ساعت ${formatJalali(new Date(), "time")}`);
        onSaved(saved);
      })
      .catch((err: unknown) => {
        setServerErrors(serverFieldErrors(err));
        toast.error("تنظیمات ذخیره نشد", errorMessage(err));
      })
      .finally(() => setBusy(false));
  };

  return (
    <form onSubmit={save} noValidate className="flex flex-col gap-4 sm:gap-5">
      <Section icon={<Gauge className="size-5" />} title="سقف‌ها" subtitle="کد از این سقف‌ها هرگز عبور نمیکند (قانون ۸). بی سقف بودجه و سقف تعداد، هیچ سروری ساخته نمیشود.">
        <Field
          label="سقف بودجه‌ی ماه"
          htmlFor="s-budget"
          hint="به یورو، برای همه‌ی حساب‌ها روی هم. خالی یعنی تعیین نشده."
          error={shown("budget_cap_eur")}
        >
          <TextInput
            id="s-budget"
            inputMode="decimal"
            latin
            value={draft.budget_cap_eur}
            onChange={(e) => set("budget_cap_eur", e.target.value)}
            invalid={Boolean(shown("budget_cap_eur"))}
            placeholder="مثلاً 150"
            suffix="€"
          />
        </Field>
        <Field label="سقف کل تعداد node" htmlFor="s-nodecap" hint="۰ تا ۲۰۰. خالی یعنی تعیین نشده." error={shown("node_cap_total")}>
          <TextInput
            id="s-nodecap"
            inputMode="numeric"
            latin
            value={draft.node_cap_total}
            onChange={(e) => set("node_cap_total", e.target.value)}
            invalid={Boolean(shown("node_cap_total"))}
            placeholder="مثلاً 10"
          />
        </Field>
        <Field label="سقف ساخت در ساعت" htmlFor="s-perhour" hint="۰ تا ۲۰ سرور تازه در هر ۶۰ دقیقه." error={shown("creations_per_hour_cap")}>
          <TextInput
            id="s-perhour"
            inputMode="numeric"
            latin
            value={draft.creations_per_hour_cap}
            onChange={(e) => set("creations_per_hour_cap", e.target.value)}
            invalid={Boolean(shown("creations_per_hour_cap"))}
          />
        </Field>
      </Section>

      <Section icon={<Server className="size-5" />} title="شکل ناوگان" subtitle="۹ جایگاه فعال + ۱ جای خالی (تصمیم D14)؛ خرید از locationهای مختلف.">
        <Field label="تعداد جایگاه هدف" htmlFor="s-slots" hint="۰ تا ۲۰۰." error={shown("target_slots")}>
          <TextInput
            id="s-slots"
            inputMode="numeric"
            latin
            value={draft.target_slots}
            onChange={(e) => set("target_slots", e.target.value)}
            invalid={Boolean(shown("target_slots"))}
          />
        </Field>
        <Field label="نوع‌های مجاز سرور" htmlFor="s-types" hint="نام نوع در هتزنر؛ Enter برای افزودن." error={shown("allowed_server_types")}>
          <TagInput
            id="s-types"
            values={draft.allowed_server_types}
            onChange={(v) => set("allowed_server_types", v)}
            suggestions={SERVER_TYPES}
            placeholder="cpx02"
            invalid={Boolean(shown("allowed_server_types"))}
          />
        </Field>
        <div className="sm:col-span-2">
          <Field
            label={
              <span className="inline-flex items-center gap-1.5">
                <MapPin className="size-4 text-brand" /> locationها
              </span>
            }
            htmlFor="s-locs"
            hint="دیتاسنترهای هتزنر برای nodeها؛ پخش شدن در چند location جلوی IP تکراری را میگیرد. ناوگان از این فهرست بیرون نمیرود: locationهایی را که از همراه اول و رایتل باز بوده‌اند ترجیح میدهد و بسته‌ها را کنار میگذارد (جدول «locationها از ایران» در صفحه‌ی nodeها)."
            error={shown("locations")}
          >
            <TagInput
              id="s-locs"
              values={draft.locations}
              onChange={(v) => set("locations", v)}
              suggestions={LOCATIONS}
              placeholder="fsn1"
              invalid={Boolean(shown("locations"))}
            />
          </Field>
        </div>
      </Section>

      <Card>
        <CardHeader icon={<FlaskConical className="size-5" />} title="حالت کار" subtitle="در حالت خشک، چرخه حالت واقعی را میخواند ولی فقط مینویسد «این کار را میکردم»." />
        <div className="p-5">
          <Toggle
            checked={draft.dry_run}
            onCheckedChange={(v) => set("dry_run", v)}
            tone="warning"
            label="حالت خشک (dry-run)"
            description="روشن: هیچ سروری ساخته یا حذف نمیشود و فقط برنامه در «نمای کلی» دیده میشود."
          />
          <AnimatePresence>
            {serverErrors.dry_run ? (
              <motion.p
                role="alert"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                className="mt-3 rounded-xl bg-danger-soft px-3 py-2 text-sm leading-7 text-fg"
              >
                {serverErrors.dry_run}
              </motion.p>
            ) : null}
          </AnimatePresence>
        </div>
      </Card>

      <AnimatePresence>
        {dirty ? (
          <motion.div
            initial={{ y: 80, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 80, opacity: 0 }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
            className="sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-20 lg:bottom-6"
          >
            <div className="flex items-center gap-3 rounded-2xl border border-line bg-surface/95 p-3 shadow-pop backdrop-blur-xl">
              <span className="min-w-0 flex-1 text-sm font-semibold text-fg">
                {hasErrors && submitted ? <span className="text-danger">بخش‌های قرمز را اصلاح کنید.</span> : "تغییرات ذخیره نشده"}
              </span>
              <Button
                variant="ghost"
                size="sm"
                icon={<Undo2 className="size-4" />}
                onClick={() => {
                  setDraft(draftFromSettings(initial));
                  setServerErrors({});
                  setSubmitted(false);
                }}
                disabled={busy}
              >
                لغو
              </Button>
              <Button type="submit" size="sm" loading={busy} icon={<Save className="size-4" />}>
                ذخیره
              </Button>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </form>
  );
}

export default function SettingsPage() {
  const settings = useFleetSettings();
  const [version, setVersion] = useState(0);
  const data = settings.data;

  return (
    <>
      <PageHeader
        title="تنظیمات ناوگان"
        subtitle="هدف‌ها، سقف‌ها و کلید توقف. هر تغییر در «رویدادها» ثبت میشود."
        eyebrow={
          <span className="inline-flex items-center gap-1.5">
            <SlidersHorizontal className="size-3.5" /> کنترل مالک
          </span>
        }
      />
      {settings.isPending ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-36 rounded-3xl" />
          <Skeleton className="h-72 rounded-3xl" />
          <Skeleton className="h-60 rounded-3xl" />
        </div>
      ) : settings.isError ? (
        <Card>
          <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />
        </Card>
      ) : data ? (
        <div className="flex flex-col gap-4 sm:gap-5">
          <KillSwitchControl on={data.kill_switch} budgetSet={data.budget_cap_eur !== null} nodeCapSet={data.node_cap_total !== null} />
          <SettingsForm key={version} initial={data} onSaved={() => setVersion((v) => v + 1)} />
          <TargetCandidates />
        </div>
      ) : null}
    </>
  );
}
