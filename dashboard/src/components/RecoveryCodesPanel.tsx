import { FileDown, KeyRound } from "lucide-react";
import { motion } from "motion/react";
import { useState } from "react";

import { downloadText } from "../lib/browser";
import { formatJalali, toFaDigits } from "../lib/format";
import { Button } from "./ui/Button";
import { CopyButton } from "./ui/Feedback";

/** Shows one-time recovery codes once, with copy and download, and asks the owner to confirm. */
export function RecoveryCodesPanel({
  codes,
  title,
  intro,
  continueLabel,
  onContinue,
}: {
  codes: string[];
  title: string;
  intro: string;
  continueLabel: string;
  onContinue: () => void;
}) {
  const [saved, setSaved] = useState(false);
  const text = codes.join("\n");
  const fileText = [
    "Proxy-Hub — کدهای بازیابی ورود",
    `ساخته‌شده: ${formatJalali(new Date(), "long")}`,
    "هر کد فقط یک بار کار میکند. این فایل را جای امنی نگه دارید.",
    "",
    ...codes,
    "",
  ].join("\n");

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-3">
        <span className="grid size-12 place-items-center rounded-2xl bg-success-soft text-success">
          <KeyRound className="size-6" />
        </span>
        <div>
          <h1 className="text-xl leading-9 font-extrabold">{title}</h1>
          <p className="text-sm text-fg-muted">{toFaDigits(codes.length)} کد یک‌بارمصرف</p>
        </div>
      </div>
      <p className="text-sm leading-7 text-fg-muted">{intro}</p>
      <ol className="grid grid-cols-2 gap-2" aria-label="کدهای بازیابی" data-ph-secret>
        {codes.map((c, i) => (
          <motion.li
            key={c}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.04 * i }}
            className="flex items-center gap-2 rounded-xl border border-line bg-surface-2 px-3 py-2.5"
          >
            <span className="w-5 text-xs text-fg-faint">{toFaDigits(i + 1)}</span>
            <code className="ltr flex-1 text-center font-mono text-[0.92rem] font-bold tracking-wider text-fg select-all">{c}</code>
          </motion.li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2">
        <CopyButton text={text} label="کپی همه" size="md" />
        <Button
          variant="secondary"
          icon={<FileDown className="size-4" />}
          onClick={() => downloadText("proxyhub-recovery-codes.txt", fileText)}
        >
          ذخیره در فایل
        </Button>
      </div>
      <label className="flex cursor-pointer items-start gap-3 rounded-2xl border border-line bg-surface-2 p-3.5 text-sm leading-7">
        <input
          type="checkbox"
          checked={saved}
          onChange={(e) => setSaved(e.target.checked)}
          className="mt-1.5 size-4 shrink-0 accent-[var(--ph-brand)]"
        />
        <span>این کدها را جای امنی (مثلاً مدیر رمز یا کاغذ) ذخیره کردم. میدانم دوباره نشان داده نمیشوند.</span>
      </label>
      <Button size="lg" block disabled={!saved} onClick={onContinue}>
        {continueLabel}
      </Button>
    </div>
  );
}
