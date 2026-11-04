// Cloudflare page helpers (pure, unit-tested): token shape, zone order, TTL text, what a pool
// change means, and the automatic pool (D35): the last filter check in plain Persian, when a
// filtered domain is deleted from Cloudflare (or why it cannot be), and the "no standby" warning.
// The server decides; these only explain.

import { formatDurationSeconds, formatJalali, formatNumber, formatRelative } from "./format";
import { checkStatusText, deleteBlockedText, deleteWaitText, POOL_ORDER, poolStateInfo, type Tone } from "./labels";
import type { CfStatus, CfZone, DomainCheck, PoolState } from "./types";

/** A Persian hint if the pasted text cannot be an API token (null if it looks right). Never echoes it. */
export function cfTokenProblem(raw: string): string | null {
  const s = raw.trim();
  if (s === "") return "توکن API ساخته‌شده در Cloudflare را بچسبانید.";
  if (/\s/.test(s)) return "توکن نباید فاصله یا خط تازه داشته باشد؛ فقط خود توکن را بچسبانید.";
  if (!/^[A-Za-z0-9_-]{20,200}$/.test(s)) return "این شکل توکن API نیست؛ توکن فقط حرف و رقم انگلیسی، خط تیره و زیرخط دارد.";
  return null;
}

/** Active first, then standby, unused, filtered, retired, deleted; by name within a group. */
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
export function poolChangeText(zone: CfZone, target: PoolState, zones: CfZone[], graceHours = 24): string {
  const to = poolStateInfo(target).label;
  const current = activeZone(zones);
  if (target === "active") {
    const swap = current && current.id !== zone.id ? ` «${current.name}» اکنون فعال است و فقط یک دامنه میتواند فعال باشد.` : "";
    return `از این پس نام‌های پروکسی پست‌های تازه روی «${zone.name}» ساخته میشوند.${swap}`;
  }
  if (target === "filtered") {
    const was = zone.pool_state === "active" ? " دیگر دامنه‌ی فعال نیست،" : "";
    return `«${zone.name}»${was} از دور خارج میشود و پس از ${formatNumber(graceHours)} ساعت از Cloudflare پاک میشود (اگر کلید توقف خاموش باشد). تا آن موقع میتوانید آن را به «آماده‌به‌کار» برگردانید.`;
  }
  if (zone.pool_state === "filtered") {
    return `پاک شدن «${zone.name}» از Cloudflare لغو میشود و دامنه به «${to}» میرود.`;
  }
  if (zone.pool_state === "active") {
    return `«${zone.name}» دیگر دامنه‌ی فعال نیست و به «${to}» میرود. نام‌های پروکسی پست‌های تازه فقط روی دامنه‌ی فعال ساخته میشوند.`;
  }
  return `«${zone.name}» به «${to}» میرود. ${poolStateInfo(target).hint}`;
}

export interface Line {
  tone: Tone;
  text: string;
}

/** The latest filter check from Iran, in one plain Persian sentence. */
export function checkSummary(check: DomainCheck | null | undefined): Line {
  if (!check) return { tone: "neutral", text: "هنوز از ایران سنجیده نشده است." };
  if (check.answered <= 0) {
    const why = check.status === "ok" ? "هیچ نقطه‌ای در ایران پاسخ نداد" : checkStatusText(check.status);
    return { tone: "neutral", text: `آخرین سنجش نتیجه نداد: ${why}. دوباره سنجیده میشود.` };
  }
  const answered = formatNumber(check.answered);
  const nets = formatNumber(check.networks_answered);
  if (check.hijacked === 0) {
    return { tone: "success", text: `باز از ایران: هر ${answered} پاسخ از ${nets} شبکه‌ی ایران درست بود.` };
  }
  const hijacked = `${formatNumber(check.hijacked)} پاسخ از ${answered} (از ${formatNumber(check.networks_hijacked)} شبکه)`;
  if (check.verdict === "filtered") {
    return { tone: "danger", text: `فیلتر در ایران: ${hijacked} پاسخ جعلی فیلتر (‎10.10.34.x‎) بود، در دو سنجش پشت سر هم.` };
  }
  return { tone: "warning", text: `${hijacked} پاسخ جعلی فیلتر (‎10.10.34.x‎) بود؛ برای اطمینان دوباره سنجیده میشود.` };
}

export interface DeletionLine {
  tone: Tone;
  /** The main sentence: when it is (or was) deleted from Cloudflare, or why it cannot be. */
  text: string;
  /** Conditions it still waits for, one Persian sentence each. */
  notes: string[];
}

/** For a filtered or deleted zone: the red line under it. Null for every other zone. */
export function deletionLine(zone: CfZone, now: Date = new Date()): DeletionLine | null {
  if (zone.pool_state === "deleted") {
    const when = zone.deleted_at ? `${formatJalali(zone.deleted_at, "datetime")} (${formatRelative(zone.deleted_at, now)})` : "";
    return { tone: "neutral", text: when ? `از Cloudflare پاک شد: ${when}.` : "از Cloudflare پاک شد.", notes: [] };
  }
  if (zone.pool_state !== "filtered") return null;
  if (zone.delete_blocked) return { tone: "danger", text: deleteBlockedText(zone.delete_blocked), notes: [] };
  const notes = (zone.delete_waiting ?? []).map(deleteWaitText);
  if (!zone.delete_after) return { tone: "danger", text: "از دور خارج شد؛ زمان پاک شدن از Cloudflare هنوز معلوم نیست.", notes };
  const after = new Date(zone.delete_after);
  if (Number.isNaN(after.getTime())) return { tone: "danger", text: "از دور خارج شد.", notes };
  const date = formatJalali(zone.delete_after, "datetime");
  if (after.getTime() > now.getTime()) {
    return { tone: "danger", text: `از دور خارج شد و ${date} (${formatRelative(zone.delete_after, now)}) از Cloudflare پاک میشود.`, notes };
  }
  if (notes.length > 0) return { tone: "danger", text: `مهلت ${date} تمام شده، ولی هنوز پاک نمیشود:`, notes };
  return { tone: "danger", text: `مهلت ${date} تمام شده؛ در چند دقیقه‌ی آینده از Cloudflare پاک میشود.`, notes };
}

/** The "no standby domain left" warning: only once the pool is in use (an active or filtered domain). */
export function noStandbyLeft(s: Pick<CfStatus, "pool">): boolean {
  return Boolean(s.pool?.in_use) && (s.pool?.standby_usable ?? 0) === 0;
}
