import { CircleCheck, CircleX, Info, TriangleAlert, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Toast } from "radix-ui";

import { cn } from "../../lib/browser";
import { dismissToast, useToasts, type ToastKind } from "../../lib/toast";

const ICON: Record<ToastKind, { el: typeof Info; cls: string }> = {
  success: { el: CircleCheck, cls: "text-success bg-success-soft" },
  error: { el: CircleX, cls: "text-danger bg-danger-soft" },
  info: { el: Info, cls: "text-info bg-info-soft" },
  warning: { el: TriangleAlert, cls: "text-warning bg-warning-soft" },
};

export function Toaster() {
  const items = useToasts();
  return (
    <Toast.Provider swipeDirection="up" duration={5500} label="پیام‌ها">
      <AnimatePresence initial={false}>
        {items.map((t) => {
          const Icon = ICON[t.kind].el;
          return (
            <Toast.Root
              key={t.id}
              asChild
              forceMount
              type={t.kind === "error" ? "foreground" : "background"}
              onOpenChange={(open) => {
                if (!open) dismissToast(t.id);
              }}
            >
              <motion.li
                layout
                initial={{ opacity: 0, y: -18, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -12, scale: 0.96, transition: { duration: 0.16 } }}
                transition={{ type: "spring", stiffness: 420, damping: 32 }}
                className="pointer-events-auto flex w-full items-start gap-3 rounded-2xl border border-line bg-surface/95 p-3.5 shadow-pop backdrop-blur-md"
              >
                <span className={cn("grid size-9 shrink-0 place-items-center rounded-xl", ICON[t.kind].cls)}>
                  <Icon className="size-5" />
                </span>
                <div className="min-w-0 flex-1 pt-0.5">
                  <Toast.Title className="text-sm leading-6 font-bold text-fg">{t.title}</Toast.Title>
                  {t.description ? (
                    <Toast.Description className="text-[0.82rem] leading-6 text-fg-muted">{t.description}</Toast.Description>
                  ) : null}
                </div>
                <Toast.Close
                  className="grid size-8 shrink-0 place-items-center rounded-lg text-fg-faint hover:bg-surface-3 hover:text-fg"
                  aria-label="بستن"
                >
                  <X className="size-4" />
                </Toast.Close>
              </motion.li>
            </Toast.Root>
          );
        })}
      </AnimatePresence>
      <Toast.Viewport className="pointer-events-none fixed inset-x-0 top-0 z-[60] mx-auto flex w-full max-w-md flex-col gap-2 p-3 pt-[max(0.75rem,env(safe-area-inset-top))] outline-none sm:top-4" />
    </Toast.Provider>
  );
}
