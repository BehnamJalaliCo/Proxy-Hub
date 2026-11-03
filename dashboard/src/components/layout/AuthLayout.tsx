import { Lock } from "lucide-react";
import { motion } from "motion/react";
import type { ReactNode } from "react";

import { Logo, ThemeSwitch } from "./Brand";

/** Full-screen frame for /login and /setup: animated glow, centred card. */
export function AuthLayout({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <div className="relative flex min-h-dvh flex-col overflow-clip bg-bg">
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-clip">
        <div className="absolute -top-40 -right-32 size-[34rem] animate-float-a rounded-full bg-[radial-gradient(circle,var(--ph-bg-glow-1),transparent_65%)] blur-2xl" />
        <div className="absolute -bottom-48 -left-40 size-[38rem] animate-float-b rounded-full bg-[radial-gradient(circle,var(--ph-bg-glow-2),transparent_65%)] blur-2xl" />
        <div className="absolute inset-0 bg-[radial-gradient(var(--ph-line)_1px,transparent_1px)] [background-size:22px_22px] [mask-image:radial-gradient(ellipse_at_center,black_30%,transparent_75%)]" />
      </div>

      <header className="relative z-10 flex items-center justify-between px-4 pt-[max(1rem,env(safe-area-inset-top))] sm:px-8 sm:pt-6">
        <Logo />
        <ThemeSwitch />
      </header>

      <main className="relative z-10 flex flex-1 items-start justify-center px-4 py-8 sm:items-center sm:py-12">
        <motion.div
          initial={{ opacity: 0, y: 18, scale: 0.985 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
          className={wide ? "w-full max-w-2xl" : "w-full max-w-md"}
        >
          {children}
        </motion.div>
      </main>

      <footer className="relative z-10 flex items-center justify-center gap-2 px-4 pb-[max(1.25rem,env(safe-area-inset-bottom))] text-xs text-fg-faint">
        <Lock className="size-3.5" />
        <span>اتصال امن · ورود دومرحله‌ای · بدون نگهداری IP کاربران</span>
      </footer>
    </div>
  );
}
