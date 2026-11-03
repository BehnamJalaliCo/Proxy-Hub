// Cloudflare page helpers (pure, unit-tested): token shape, zone order, TTL text and what a pool
// change means. The server decides; these only explain before sending.

import { formatDurationSeconds } from "./format";
import { POOL_ORDER, poolStateInfo } from "./labels";
import type { CfZone, PoolState } from "./types";

/** A Persian hint if the pasted text cannot be an API token (null if it looks right). Never echoes it. */
export function cfTokenProblem(raw: string): string | null {
  const s = raw.trim();
  if (s === "") return "توکن API ساخته‌شده در Cloudflare را بچسبانید.";
  if (/\s/.test(s)) return "توکن نباید فاصله یا خط تازه داشته باشد؛ فقط خود توکن را بچسبانید.";
  if (!/^[A-Za-z0-9_-]{20,200}$/.test(s)) return "این شکل توکن API نیست؛ توکن فقط حرف و رقم انگلیسی، خط تیره و زیرخط دارد.";
  return null;
}

/** Active first, then standby, unused, filtered, retired; by name within a group. */
export function sortZones(zones: CfZone[]): CfZone[] {
  const rank = (z: CfZone) => {
    const i = POOL_ORDER.indexOf(z.pool_state);
    return i < 0 ? POOL_ORDER.length : i;
  };
  return [...zones].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name, "en"));
}

export function activeZone(zones: CfZone[]): CfZone | undefined {
  return zones.find((z) => z.pool_state === "active");
}

/** 1 is Cloudflare's "automatic" (300 s); anything else is seconds. */
export function ttlText(ttl: number): string {
  if (ttl === 1) return "خودکار";
  return formatDurationSeconds(ttl);
}

/** What the owner is about to do, in one Persian sentence (shown in the confirmation). */
export function poolChangeText(zone: CfZone, target: PoolState, zones: CfZone[]): string {
  const to = poolStateInfo(target).label;
  const current = activeZone(zones);
  if (target === "active") {
    const swap = current && current.id !== zone.id ? ` «${current.name}» اکنون فعال است و فقط یک دامنه میتواند فعال باشد.` : "";
    return `از این پس نام‌های پروکسی پست‌های تازه روی «${zone.name}» ساخته میشوند.${swap}`;
  }
  if (zone.pool_state === "active") {
    return `«${zone.name}» دیگر دامنه‌ی فعال نیست و به «${to}» میرود. نام‌های پروکسی پست‌های تازه فقط روی دامنه‌ی فعال ساخته میشوند.`;
  }
  return `«${zone.name}» به «${to}» میرود. ${poolStateInfo(target).hint}`;
}
