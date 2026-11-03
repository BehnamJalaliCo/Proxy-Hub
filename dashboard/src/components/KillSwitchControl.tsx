import { useQueryClient } from "@tanstack/react-query";
import { OctagonPause, Power, ShieldCheck } from "lucide-react";
import { motion } from "motion/react";
import { useState } from "react";

import { cn } from "../lib/browser";
import { errorMessage } from "../lib/api";
import * as api from "../lib/endpoints";
import { keys } from "../lib/queries";
import { toast } from "../lib/toast";
import type { FleetSettings, Overview } from "../lib/types";
import { Button } from "./ui/Button";
import { Card } from "./ui/Card";
import { ConfirmDialog } from "./ui/Dialogs";

/** The emergency stop. Turning it ON is one confirmation; turning it OFF needs an explicit tick. */
export function KillSwitchControl({ on, budgetSet, nodeCapSet }: { on: boolean; budgetSet: boolean; nodeCapSet: boolean }) {
  const qc = useQueryClient();
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const target = !on;

  const apply = () => {
    setBusy(true);
    api
      .setKillSwitch(target)
      .then((res) => {
        qc.setQueryData<FleetSettings>(keys.settings, (old) => (old ? { ...old, kill_switch: res.kill_switch } : old));
        qc.setQueryData<Overview>(keys.overview, (old) => (old ? { ...old, kill_switch: res.kill_switch } : old));
        void qc.invalidateQueries({ queryKey: keys.overview });
        void qc.invalidateQueries({ queryKey: ["events"] });
        if (res.kill_switch) toast.warning("کلید توقف روشن شد", "هیچ سروری ساخته یا حذف نمیشود.");
        else toast.success("کلید توقف خاموش شد", "پنل در محدوده‌ی سقف‌ها کار میکند.");
        setAsk(false);
      })
      .catch((e: unknown) => toast.error("کلید توقف تغییر نکرد", errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <Card className={cn("relative overflow-clip", on ? "border-danger/30" : "border-success/30")}>
      <div
        aria-hidden
        className={cn(
          "absolute inset-0",
          on
            ? "bg-[radial-gradient(90%_120%_at_100%_0%,var(--ph-danger-soft),transparent_65%)]"
            : "bg-[radial-gradient(90%_120%_at_100%_0%,var(--ph-success-soft),transparent_65%)]",
        )}
      />
      <div className="relative flex flex-col gap-5 p-5 sm:flex-row sm:items-center sm:p-6">
        <motion.span
          key={String(on)}
          initial={{ scale: 0.7, rotate: -15 }}
          animate={{ scale: 1, rotate: 0 }}
          transition={{ type: "spring", stiffness: 300, damping: 16 }}
          className={cn("grid size-16 shrink-0 place-items-center rounded-3xl text-white", on ? "bg-danger shadow-[0_12px_30px_-10px_var(--ph-danger)]" : "bg-success shadow-[0_12px_30px_-10px_var(--ph-success)]")}
        >
          {on ? <OctagonPause className="size-8" /> : <ShieldCheck className="size-8" />}
        </motion.span>
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-extrabold text-fg">
            کلید توقف: <span className={on ? "text-danger" : "text-success"}>{on ? "روشن" : "خاموش"}</span>
          </h2>
          <p className="mt-1 text-sm leading-7 text-fg-muted">
            {on
              ? "همه‌ی ساخت و حذف سرورها متوقف است. هر وقت لازم شد، با یک دکمه همه‌چیز را متوقف کنید."
              : "پنل میتواند در محدوده‌ی سقف بودجه و سقف تعداد، سرور بسازد و حذف کند."}
          </p>
        </div>
        <Button
          variant={on ? "success" : "danger"}
          size="lg"
          icon={<Power className="size-5" />}
          onClick={() => setAsk(true)}
          className="sm:min-w-44"
        >
          {on ? "خاموش کردن" : "روشن کردن (توقف)"}
        </Button>
      </div>

      <ConfirmDialog
        open={ask}
        onOpenChange={(v) => {
          if (!busy) setAsk(v);
        }}
        title={target ? "روشن کردن کلید توقف؟" : "خاموش کردن کلید توقف؟"}
        description={
          target
            ? "از همین لحظه هیچ سروری ساخته یا حذف نمیشود. سرورهای موجود کار میکنند. هر وقت خواستید دوباره خاموشش کنید."
            : "با خاموش شدن، پنل اجازه دارد در محدوده‌ی سقف‌ها سرور بسازد و حذف کند و هزینه کند."
        }
        confirmLabel={target ? "روشن کن" : "خاموش کن"}
        tone={target ? "danger" : "success"}
        icon={<Power className="size-6" />}
        busy={busy}
        onConfirm={apply}
        {...(target ? {} : { acknowledge: "میدانم که با خاموش کردن کلید توقف، پنل میتواند در محدوده‌ی سقف‌ها سرور بسازد و هزینه کند." })}
      >
        {!target && (!budgetSet || !nodeCapSet) ? (
          <p className="rounded-xl bg-warning-soft px-3 py-2 text-[0.84rem] leading-7 text-fg">
            {!budgetSet ? "سقف بودجه " : ""}
            {!budgetSet && !nodeCapSet ? "و " : ""}
            {!nodeCapSet ? "سقف تعداد node " : ""}
            تعیین نشده است؛ تا تعیین نشود، حتی با کلید خاموش هیچ سروری ساخته نمیشود.
          </p>
        ) : null}
      </ConfirmDialog>
    </Card>
  );
}
