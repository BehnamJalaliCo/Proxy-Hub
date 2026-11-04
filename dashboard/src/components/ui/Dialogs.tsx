import { X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { AlertDialog, Dialog } from "radix-ui";
import { useState, useSyncExternalStore, type ReactNode } from "react";

import { cn } from "../../lib/browser";
import { Button } from "./Button";

const WIDE = "(min-width: 640px)";

function subscribeWide(cb: () => void): () => void {
  const mq = window.matchMedia(WIDE);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

/** True on screens where a drawer slides in from the side instead of up from the bottom. */
function useWide(): boolean {
  return useSyncExternalStore(
    subscribeWide,
    () => window.matchMedia(WIDE).matches,
    () => true,
  );
}

/** Side panel on the end edge (left in RTL) on larger screens, bottom sheet on phones. For detail
 * views next to a list (a node's events). */
export function Drawer({
  open,
  onOpenChange,
  title,
  description,
  icon,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  children: ReactNode;
}) {
  const wide = useWide();
  const motionProps = wide
    ? { initial: { x: "-100%" }, animate: { x: 0 }, exit: { x: "-100%" } }
    : { initial: { y: "100%" }, animate: { y: 0 }, exit: { y: "100%" } };
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <AnimatePresence>
        {open ? (
          <Dialog.Portal forceMount>
            <Dialog.Overlay asChild forceMount>
              <motion.div {...overlay} className="fixed inset-0 z-50 bg-[rgb(6_8_18/0.5)] backdrop-blur-[2px]" />
            </Dialog.Overlay>
            <Dialog.Content asChild forceMount>
              <motion.div
                {...motionProps}
                transition={{ type: "spring", stiffness: 380, damping: 38 }}
                className={cn(
                  "fixed z-50 flex flex-col border-line bg-surface shadow-pop outline-none",
                  wide
                    ? "inset-y-0 end-0 w-[min(30rem,92vw)] border-s"
                    : "inset-x-0 bottom-0 max-h-[88dvh] rounded-t-[1.75rem] border-t pb-[env(safe-area-inset-bottom)]",
                )}
              >
                {!wide ? <div className="mx-auto mt-3 h-1.5 w-12 shrink-0 rounded-full bg-line-strong" aria-hidden /> : null}
                <div className="flex items-start gap-3 border-b border-line px-5 pt-4 pb-4 sm:pt-6">
                  {icon ? <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">{icon}</span> : null}
                  <div className="min-w-0 flex-1">
                    <Dialog.Title className="text-lg leading-8 font-bold text-fg">{title}</Dialog.Title>
                    <Dialog.Description className={cn("text-sm leading-7 text-fg-muted", !description && "sr-only")}>
                      {description ?? title}
                    </Dialog.Description>
                  </div>
                  <Dialog.Close asChild>
                    <button
                      type="button"
                      className="grid size-9 shrink-0 place-items-center rounded-xl text-fg-faint hover:bg-surface-3 hover:text-fg"
                      aria-label="بستن"
                    >
                      <X className="size-5" />
                    </button>
                  </Dialog.Close>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
              </motion.div>
            </Dialog.Content>
          </Dialog.Portal>
        ) : null}
      </AnimatePresence>
    </Dialog.Root>
  );
}

const overlay = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: 0.18 },
};

const panel = {
  initial: { opacity: 0, y: 32, scale: 0.98 },
  animate: { opacity: 1, y: 0, scale: 1 },
  exit: { opacity: 0, y: 24, scale: 0.98 },
  transition: { type: "spring" as const, stiffness: 380, damping: 34 },
};

const PANEL_CLASS =
  "pointer-events-auto fixed inset-x-0 bottom-0 z-50 max-h-[92dvh] overflow-y-auto rounded-t-[1.75rem] border border-line bg-surface p-5 pb-[max(1.25rem,env(safe-area-inset-bottom))] shadow-pop outline-none sm:relative sm:inset-auto sm:w-[min(32rem,calc(100vw-2rem))] sm:rounded-[1.75rem] sm:p-6";

/** Focus the first form field instead of the close button when a dialog opens. */
function focusFirstField(e: Event): void {
  const root = e.currentTarget instanceof HTMLElement ? e.currentTarget : null;
  const field = root?.querySelector<HTMLElement>("input:not([type=checkbox]), textarea, select");
  if (field) {
    e.preventDefault();
    field.focus();
  }
}

/** Bottom sheet on phones, centred dialog on larger screens. */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  icon,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <AnimatePresence>
        {open ? (
          <Dialog.Portal forceMount>
            <Dialog.Overlay asChild forceMount>
              <motion.div {...overlay} className="fixed inset-0 z-50 bg-[rgb(6_8_18/0.55)] backdrop-blur-[3px]" />
            </Dialog.Overlay>
            <div className="pointer-events-none fixed inset-0 z-50 sm:grid sm:place-items-center">
              <Dialog.Content asChild forceMount onOpenAutoFocus={focusFirstField}>
                <motion.div {...panel} className={PANEL_CLASS}>
                  <div className="mx-auto mb-4 h-1.5 w-12 rounded-full bg-line-strong sm:hidden" aria-hidden />
                  <div className="mb-5 flex items-start gap-3">
                    {icon ? <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-brand-soft text-brand">{icon}</span> : null}
                    <div className="min-w-0 flex-1">
                      <Dialog.Title className="text-lg leading-8 font-bold text-fg">{title}</Dialog.Title>
                      <Dialog.Description className={cn("text-sm leading-7 text-fg-muted", !description && "sr-only")}>
                        {description ?? title}
                      </Dialog.Description>
                    </div>
                    <Dialog.Close asChild>
                      <button
                        type="button"
                        className="grid size-9 shrink-0 place-items-center rounded-xl text-fg-faint hover:bg-surface-3 hover:text-fg"
                        aria-label="بستن"
                      >
                        <X className="size-5" />
                      </button>
                    </Dialog.Close>
                  </div>
                  {children}
                </motion.div>
              </Dialog.Content>
            </div>
          </Dialog.Portal>
        ) : null}
      </AnimatePresence>
    </Dialog.Root>
  );
}

/** Confirmation for consequential actions. `acknowledge` adds a checkbox that must be ticked. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  tone = "primary",
  icon,
  acknowledge,
  busy = false,
  onConfirm,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger" | "success";
  icon?: ReactNode;
  acknowledge?: string;
  busy?: boolean;
  onConfirm: () => void;
  children?: ReactNode;
}) {
  const [ack, setAck] = useState(false);
  const iconTone = tone === "danger" ? "bg-danger-soft text-danger" : tone === "success" ? "bg-success-soft text-success" : "bg-brand-soft text-brand";
  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) setAck(false);
        onOpenChange(v);
      }}
    >
      <AnimatePresence>
        {open ? (
          <AlertDialog.Portal forceMount>
            <AlertDialog.Overlay asChild forceMount>
              <motion.div {...overlay} className="fixed inset-0 z-50 bg-[rgb(6_8_18/0.55)] backdrop-blur-[3px]" />
            </AlertDialog.Overlay>
            <div className="pointer-events-none fixed inset-0 z-50 sm:grid sm:place-items-center">
              <AlertDialog.Content asChild forceMount>
                <motion.div {...panel} className={PANEL_CLASS}>
                  <div className="mx-auto mb-4 h-1.5 w-12 rounded-full bg-line-strong sm:hidden" aria-hidden />
                  <div className="flex items-start gap-3">
                    {icon ? <span className={cn("grid size-12 shrink-0 place-items-center rounded-2xl", iconTone)}>{icon}</span> : null}
                    <div className="min-w-0 flex-1">
                      <AlertDialog.Title className="text-lg leading-8 font-bold text-fg">{title}</AlertDialog.Title>
                      <AlertDialog.Description className="mt-1 text-sm leading-7 text-fg-muted">{description}</AlertDialog.Description>
                    </div>
                  </div>
                  {children ? <div className="mt-4">{children}</div> : null}
                  {acknowledge ? (
                    <label className="mt-4 flex cursor-pointer items-start gap-3 rounded-2xl border border-line bg-surface-2 p-3.5 text-sm leading-7 text-fg">
                      <input
                        type="checkbox"
                        checked={ack}
                        onChange={(e) => setAck(e.target.checked)}
                        className="mt-1.5 size-4 shrink-0 accent-[var(--ph-brand)]"
                      />
                      <span>{acknowledge}</span>
                    </label>
                  ) : null}
                  <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-start">
                    <AlertDialog.Cancel asChild>
                      <Button variant="secondary" disabled={busy}>
                        انصراف
                      </Button>
                    </AlertDialog.Cancel>
                    <Button
                      variant={tone}
                      loading={busy}
                      disabled={acknowledge !== undefined && !ack}
                      onClick={onConfirm}
                    >
                      {confirmLabel}
                    </Button>
                  </div>
                </motion.div>
              </AlertDialog.Content>
            </div>
          </AlertDialog.Portal>
        ) : null}
      </AnimatePresence>
    </AlertDialog.Root>
  );
}
