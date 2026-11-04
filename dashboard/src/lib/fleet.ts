// The live fleet (decision D14: 9 slots + 1 spare) on the nodes page: types of GET /api/fleet/slots,
// the API call, the query hook, Persian words and pure helpers. Addresses here are our own servers'
// (rule 7 is about users: nothing of theirs ever reaches this page).

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing, request } from "./api";
import { reasonLabel, type Tone } from "./labels";

// --- types --------------------------------------------------------------------------------------------

export type FleetMode = "live" | "dry_run" | "stopped";
export type SlotStatus = "healthy" | "settling" | "starting" | "replacing" | "unhealthy" | "burned" | "empty";

export interface SlotNode {
  id: string;
  name: string;
  /** A NodeState; typed wide so a new state on the server is shown, not hidden. */
  state: string;
  location: string;
  server_type: string;
  /** Our server's own address. */
  ipv4: string | null;
  verdict: string | null;
  verdict_at: string | null;
  /** Blocked from Iran (burned, or a fresh "blocked" verdict). */
  blocked: boolean;
  created_at: string | null;
  ready_at: string | null;
  state_since: string | null;
  retire_reason: string | null;
}

export interface SlotPlan {
  action: string | null;
  allowed: boolean;
  reasons: string[];
}

export interface FleetSlot {
  slot: number;
  /** slot<N>.<active domain>, or null without an active domain (links then use the IP). */
  dns_name: string | null;
  /** A SlotStatus; typed wide so a new status on the server is shown, not hidden. */
  status: string;
  /** The node the slot's DNS name follows. */
  node: SlotNode | null;
  /** A replacement on its way. */
  replacement: SlotNode | null;
  others: SlotNode[];
  leaving: SlotNode[];
  plan: SlotPlan | null;
}

export interface FleetAlertInfo {
  code: string;
  since: string | null;
  last_sent_at: string | null;
}

export interface FleetSlots {
  /** A FleetMode; typed wide like the statuses. */
  mode: string;
  target_slots: number;
  spare_slots: number;
  node_cap_total: number | null;
  live_fleet_nodes: number;
  active_domain: string | null;
  plan_at: string | null;
  slots: FleetSlot[];
  spare: { in_use: boolean; slots: number[] };
  excess: SlotNode[];
  burned_ips: number;
  burned_prefixes: number;
  alerts: FleetAlertInfo[];
}

// --- API ----------------------------------------------------------------------------------------------

export const fleetKeys = { slots: ["fleet", "slots"] as const };

export const getFleetSlots = (signal?: AbortSignal) =>
  request<FleetSlots>("GET", "/api/fleet/slots", undefined, signal ? { signal } : {});

/** Every 15 s, but not while the route does not exist on this panel yet. */
export const useFleetSlots = () =>
  useQuery({
    queryKey: fleetKeys.slots,
    queryFn: ({ signal }) => getFleetSlots(signal),
    refetchInterval: (query) => (isEndpointMissing(query.state.error) ? false : 15_000),
  });

// --- words --------------------------------------------------------------------------------------------

export interface Info {
  label: string;
  tone: Tone;
  hint: string;
}

export const FLEET_MODE: Record<FleetMode, Info> = {
  live: {
    label: "خودکار (واقعی)",
    tone: "success",
    hint: "ناوگان خودش سرور میسازد، جایگزین میکند و حذف میکند؛ همیشه زیر سقف بودجه و سقف تعداد node.",
  },
  dry_run: {
    label: "حالت خشک",
    tone: "info",
    hint: "ناوگان فقط مینویسد چه میکرد؛ هیچ سروری ساخته یا حذف نمیشود. برای روشن کردن، در «تنظیمات» حالت خشک را خاموش کنید.",
  },
  stopped: {
    label: "متوقف",
    tone: "warning",
    hint: "کلید توقف روشن است (یا تنظیمات خراب است): هیچ سروری ساخته یا حذف نمیشود.",
  },
};

export function modeInfo(mode: string): Info {
  return (FLEET_MODE as Record<string, Info | undefined>)[mode] ?? { label: mode, tone: "neutral", hint: "" };
}

export const SLOT_STATUS: Record<SlotStatus, Info> = {
  healthy: { label: "سالم", tone: "success", hint: "node این جایگاه آماده است و نامش به آن اشاره میکند." },
  settling: {
    label: "جابه‌جایی",
    tone: "info",
    hint: "سرور تازه آماده است؛ پس از چند دقیقه سرور قبلی بازنشسته میشود.",
  },
  starting: { label: "در حال ساخت", tone: "info", hint: "سرور این جایگاه ساخته و آماده میشود." },
  replacing: {
    label: "جایگزینی",
    tone: "warning",
    hint: "اول سرور تازه در location دیگری ساخته میشود، بعد قبلی حذف میشود.",
  },
  unhealthy: {
    label: "ناسالم",
    tone: "danger",
    hint: "عامل گزارش سالم نمیفرستد. ۱۵ دقیقه فرصت ترمیم، بعد جایگزین ساخته میشود.",
  },
  burned: { label: "سوخته", tone: "danger", hint: "از ایران بسته است؛ جایگزین در location دیگر ساخته میشود." },
  empty: { label: "خالی", tone: "neutral", hint: "هنوز سروری ندارد." },
};

export function slotStatusInfo(status: string): Info {
  return (SLOT_STATUS as Record<string, Info | undefined>)[status] ?? { label: status, tone: "neutral", hint: "" };
}

/** Why the slot is as it is, from the fleet loop's latest decision (Persian, in order). */
export function planNote(slot: Pick<FleetSlot, "plan" | "status">, mode: string): string | null {
  const p = slot.plan;
  if (!p) return null;
  if (p.action === "create") {
    if (p.allowed) return mode === "live" ? "ساخت سرور تازه درخواست شد." : "در حالت واقعی، سرور تازه ساخته میشد.";
    const reasons = p.reasons.filter((r) => r !== "kill_switch_on" || mode !== "stopped");
    const words = reasons.map(reasonLabel);
    return words.length ? `سرور تازه لازم است، ولی: ${words.join("، ")}` : null;
  }
  if (p.action === "none" && p.reasons.length && slot.status !== "healthy") return p.reasons.map(reasonLabel).join("، ");
  return null;
}

/** Counts for the summary tiles. */
export function slotCounts(data: Pick<FleetSlots, "slots">): { healthy: number; working: number; trouble: number; empty: number } {
  let healthy = 0;
  let working = 0;
  let trouble = 0;
  let empty = 0;
  for (const s of data.slots) {
    if (s.status === "healthy") healthy += 1;
    else if (s.status === "settling" || s.status === "starting" || s.status === "replacing") working += 1;
    else if (s.status === "unhealthy" || s.status === "burned") trouble += 1;
    else empty += 1;
  }
  return { healthy, working, trouble, empty };
}

/** The node to show as the slot's server: the one its name follows, else what is on its way. */
export function shownNode(slot: Pick<FleetSlot, "node" | "replacement">): SlotNode | null {
  return slot.node ?? slot.replacement;
}

/** The single "spare" (10th server) as the owner sees it: in use while a slot holds two servers. */
export function spareText(data: Pick<FleetSlots, "spare" | "slots">, faNumber: (n: number) => string): string {
  if (!data.spare.in_use) return "آزاد؛ برای جایگزینی بعدی آماده است.";
  if (data.spare.slots.length === 0) return "در کار است.";
  return `در کار جایگاه ${data.spare.slots.map(faNumber).join("، ")}.`;
}
