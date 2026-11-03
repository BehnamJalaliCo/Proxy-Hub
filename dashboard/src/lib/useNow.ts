import { useEffect, useState } from "react";

/** The current time, refreshed every `ms` while `active` (countdowns, "now" markers). */
export function useNow(ms: number, active = true): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(new Date()), ms);
    return () => window.clearInterval(t);
  }, [ms, active]);
  return now;
}
