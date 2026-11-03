import { motion } from "motion/react";
import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  actions,
  eyebrow,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:mb-7 sm:flex-row sm:items-end sm:justify-between">
      <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35 }}>
        {eyebrow ? <div className="mb-1.5 text-xs font-semibold text-brand">{eyebrow}</div> : null}
        <h1 className="text-2xl leading-10 font-extrabold tracking-tight text-fg sm:text-[1.9rem]">{title}</h1>
        {subtitle ? <p className="mt-0.5 text-sm leading-7 text-fg-muted">{subtitle}</p> : null}
      </motion.div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
