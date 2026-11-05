// The owner's two cleaning buttons (panel/proxyhub/api/routes_cleanup.py): endpoints, types and the
// pure helpers that turn the server's counts into Persian lines. Unit-tested in cleanup.test.ts.
//
// «پاک کردن پایان‌یافته‌ها» removes ENDED rows from the panel's database only (deleted nodes and
// what hangs on them, ended Reality configs, retired sponsor registrations, finished test-channel
// post rows, finished outbox jobs). «پاک کردن کانال آزمایشی» empties the TEST Telegram channel
// (decision D44); the main channel is never touched.

import { request } from "./api";
import { formatNumber, toFaDigits } from "./format";

// --- «پاک کردن پایان‌یافته‌ها» ---------------------------------------------------------------------

export interface EndedPurge {
  dry_run: boolean;
  min_age_hours: number;
  cutoff: string;
  /** Rows per table (every table the purge may touch, zero included). */
  removed: Record<string, number>;
  total: number;
  /** Ended things left in place, by kind and reason. */
  kept: Record<string, Record<string, number>>;
  ip_history_unlinked: number;
}

export const previewEndedPurge = (min_age_hours: number) =>
  request<EndedPurge>("POST", "/api/cleanup/ended/preview", { min_age_hours });

export const purgeEnded = (min_age_hours: number) =>
  request<EndedPurge>("POST", "/api/cleanup/ended", { min_age_hours });

export const MIN_AGE_OPTIONS: readonly { value: string; hours: number; label: string }[] = [
  { value: "1", hours: 1, label: "۱ ساعت" },
  { value: "24", hours: 24, label: "۱ روز" },
  { value: "168", hours: 168, label: "۷ روز" },
];

/** What the owner sees for each kind of row: tables grouped, in this order. */
const GROUPS: readonly { key: string; label: string; tables: readonly string[] }[] = [
  { key: "nodes", label: "node حذف‌شده", tables: ["nodes"] },
  { key: "mtproto", label: "پروکسی MTProto همان nodeها", tables: ["mtproto_proxies"] },
  { key: "reality", label: "کانفیگ Reality پایان‌یافته", tables: ["reality_configs"] },
  {
    key: "probes",
    label: "نمونه‌ی سنجش از ایران",
    tables: ["probe_measurements", "probe_network_results", "probe_rounds", "probe_node_state", "probe_location_reach"],
  },
  {
    key: "node_rows",
    label: "سابقه‌ی دیگر nodeها (عامل، Telemt، رویداد، مصرف)",
    tables: [
      "node_events",
      "agent_nodes",
      "agent_certificates",
      "agent_registration_tokens",
      "agent_node_reports",
      "telemt_node_configs",
      "telemt_users",
      "node_operations",
      "reality_node_status",
      "reality_keys",
      "reality_usage",
    ],
  },
  { key: "sponsor", label: "ثبت اسپانسر بازنشسته", tables: ["mtproxy_registrations"] },
  { key: "sponsor_rows", label: "سابقه‌ی همان ثبت‌ها", tables: ["sponsor_flows", "mtproxy_stats"] },
  { key: "posts", label: "پست کانال آزمایشی (ردیف پنل)", tables: ["posts"] },
  { key: "post_rows", label: "ردیف‌های دیگر همان پست‌ها", tables: ["post_links", "channel_posts"] },
  { key: "outbox", label: "کار تمام‌شده‌ی صف ارسال", tables: ["outbox"] },
];

export interface PurgeLine {
  key: string;
  label: string;
  count: number;
}

/** The counts as Persian lines (empty groups left out). A table the dashboard does not know yet
 * (a newer server) is added to the last line instead of being lost. */
export function purgeLines(removed: Record<string, number>): PurgeLine[] {
  const known = new Set(GROUPS.flatMap((g) => g.tables));
  const lines = GROUPS.map((g) => ({ key: g.key, label: g.label, count: g.tables.reduce((n, t) => n + (removed[t] ?? 0), 0) }));
  const unknown = Object.entries(removed).reduce((n, [t, c]) => (known.has(t) ? n : n + c), 0);
  if (unknown > 0) lines.push({ key: "other", label: "ردیف‌های دیگر", count: unknown });
  return lines.filter((l) => l.count > 0);
}

const KEPT_FA: Record<string, Record<string, string>> = {
  nodes: {
    too_recent: "تازه حذف شده‌اند (کمتر از کمینه‌ی عمر)",
    billed_this_month: "هزینه‌ی این ماهشان هنوز در سقف بودجه شمرده میشود؛ از آغاز ماه بعد پاک میشوند",
    location_reach: "سنجش‌های هفته‌ی اخیرشان از ایران هنوز برای انتخاب location لازم است",
    dns_record: "رکورد DNS آن‌ها هنوز در Cloudflare است و چرخه‌ی DNS باید اول پاکش کند",
    reality_in_use: "کانفیگ Reality زنده‌ای هنوز به آن‌ها وابسته است",
    in_recent_post: "پستی تازه هنوز لینکشان را نشان میدهد و باید علامت پایان بگیرد",
    cost_ledger: "در دفتر هزینه ثبت شده‌اند (دفتر هزینه هرگز پاک نمیشود)",
  },
  reality_configs: {
    too_recent: "تازه تمام شده‌اند",
    in_recent_post: "پستی تازه هنوز آن‌ها را فعال نشان میدهد و باید علامت پایان بگیرد",
  },
  registrations: {
    too_recent: "تازه بازنشسته شده‌اند",
    proxy_exists: "پروکسی‌شان هنوز روی nodeی هست",
    still_in_mtproxybot: "هنوز در فهرست ‎@MTProxybot‎ است؛ اول از آنجا پاک میشود",
  },
};

const KIND_FA: Record<string, string> = { nodes: "node", reality_configs: "کانفیگ Reality", registrations: "ثبت اسپانسر" };

/** Why some ended things stay, one Persian line per kind and reason. */
export function keptLines(kept: Record<string, Record<string, number>>): string[] {
  const out: string[] = [];
  for (const [kind, reasons] of Object.entries(kept)) {
    for (const [reason, count] of Object.entries(reasons)) {
      if (!count) continue;
      const why = KEPT_FA[kind]?.[reason] ?? "هنوز در استفاده‌اند";
      out.push(`${formatNumber(count)} ${KIND_FA[kind] ?? kind} میماند: ${why}.`);
    }
  }
  return out;
}

// --- «پاک کردن کانال آزمایشی» (D44) ---------------------------------------------------------------

export interface TestChannelPreview {
  channel: { username: string; title: string; chat_id: number } | null;
  main_channel_set: boolean;
  /** Read live from Telegram: does the bot have «حذف پیام‌ها» in the test channel? */
  can_delete: boolean | null;
  message_ids: { first: number; last: number } | null;
  requests: number;
  rows: { channel_posts: number; posts: number };
  pending_sends: number;
  blocked: string | null;
  blocked_fa: string | null;
}

export interface TestChannelResult {
  channel: string;
  message_ids: { first: number; last: number };
  requests: number;
  covered: number;
  refused: number;
  finished: boolean;
  stop: string | null;
  stop_fa: string | null;
  removed: { channel_posts: number; posts: number; post_links: number; edit_jobs_closed: number };
}

export const getTestChannelCleanup = (signal?: AbortSignal) =>
  request<TestChannelPreview>("GET", "/api/cleanup/test-channel", undefined, signal ? { signal } : {});

export const cleanTestChannel = (confirm: string) =>
  request<TestChannelResult>("POST", "/api/cleanup/test-channel", { confirm });

export const CONFIRM_TEXT = "پاک کن";

/** The same rule as the server: spacing, zero-width joiners and Arabic letter forms do not matter. */
export function normalizeConfirm(value: string): string {
  return value
    .normalize("NFC")
    .replace(/ك/g, "ک")
    .replace(/ي/g, "ی")
    .replace(/(?:\s|‌|‍|‎|‏)+/g, " ")
    .trim();
}

export function confirmMatches(value: string): boolean {
  return value.length <= 40 && normalizeConfirm(value) === CONFIRM_TEXT;
}

/** One sentence about what the cleaning just did. */
export function cleanSummary(r: TestChannelResult): string {
  const parts = [
    `${formatNumber(r.requests)} درخواست به تلگرام برای پیام‌های ${toFaDigits(r.message_ids.first)} تا ${toFaDigits(r.message_ids.last)}`,
  ];
  if (r.removed.channel_posts) parts.push(`${formatNumber(r.removed.channel_posts)} پست شناخته‌شده‌ی پنل پاک شد`);
  if (r.refused) parts.push(`${formatNumber(r.refused)} پیام را تلگرام اجازه‌ی حذف نداد و ماند`);
  return `${parts.join("؛ ")}.`;
}
