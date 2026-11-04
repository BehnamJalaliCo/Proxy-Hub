// The «آمار» page's data: types of /api/analytics/*, one function per route, and the query hooks.
// Aggregates only: nothing here is about one person or holds an IP (CLAUDE.md rule 7).

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing, request } from "./api";

export type Signal = "ok" | "hot" | "near_traffic_limit";
export type SourceKind = "tracking" | "link" | "folder" | "other";

export interface ServerStat {
  node_id: string;
  name: string;
  location: string;
  server_type: string;
  state: string;
  is_test: boolean;
  month_out_bytes: number;
  month_in_bytes: number;
  /** Hetzner's own outgoing counter of the billing period. */
  period_out_bytes: number | null;
  included_bytes: number | null;
  traffic_pct: number | null;
  net_out_bps: number | null;
  cpu_avg: number | null;
  cpu_max: number | null;
  signal: Signal;
  fresh: boolean;
  sampled_at: string | null;
  cost_month_eur: string | null;
}

export interface SourceStat {
  key: string;
  kind: SourceKind;
  name: string | null;
  joins: number;
  leaves: number;
  net: number;
}

export interface ChannelHeadline {
  title: string;
  username: string;
  members: number | null;
  members_at: string | null;
  joins_today: number;
  leaves_today: number;
  net_today: number;
  joins_7d: number;
  leaves_7d: number;
  net_7d: number;
  joins_30d: number;
  leaves_30d: number;
  net_30d: number;
  tracking_since: string | null;
}

export interface Thresholds {
  traffic_warn_pct: number;
  cpu_warn_pct: number;
  cpu_window_min: number;
  alerts_enabled: boolean;
}

export interface Warning {
  kind: "traffic" | "cpu";
  node_id: string;
  name: string;
  value: number | null;
  threshold: number;
}

export interface CollectorPart {
  at?: string;
  error?: string | null;
  errors?: Record<string, string> | null;
  sampled?: number;
  nodes?: number;
  outcome?: string;
}

export interface AnalyticsSummary {
  generated_at: string;
  month: { start: string; end: string; days_covered: number };
  traffic: {
    month_out_bytes: number;
    month_in_bytes: number;
    today_out_bytes: number;
    avg_day_out_bytes: number | null;
    last_month_out_bytes: number;
    sampling_since: string | null;
    last_sample_at: string | null;
  };
  cost: {
    month_to_date_eur: string;
    traffic_overage_eur: string;
    total_month_to_date_eur: string;
    projected_month_eur: string;
    per_tb_eur: string | null;
    priced_by_upper_bound: string[];
  };
  servers: ServerStat[];
  warnings: Warning[];
  channel: { main: ChannelHeadline | null; test: ChannelHeadline | null };
  top_sources: SourceStat[];
  thresholds: Thresholds;
  collector: { nodes?: CollectorPart | null; channels?: CollectorPart | null; channel_stats?: CollectorPart | null };
}

export interface DayPoint {
  /** Tehran calendar day, "2026-11-04". */
  day: string;
  out_bytes: number | null;
  in_bytes: number | null;
  joins: number | null;
  leaves: number | null;
}

export interface AnalyticsSeries {
  days: DayPoint[];
  months: { start: string; end: string; out_bytes: number }[];
  cpu: { at: string; avg: number | null; max: number | null; nodes: number }[];
  members: { at: string; count: number }[];
  sources: SourceStat[];
}

export interface UsageBucket {
  key: string;
  avg_daily_users: number;
  user_days: number;
  bytes: number;
}

export interface AnalyticsUsage {
  has_data: boolean;
  min_users: number;
  days: { day: string; unique_users: number; nodes: number }[];
  by_country: UsageBucket[];
  by_region: UsageBucket[];
  by_asn: UsageBucket[];
  per_user_mb: { day: string | null; p50: number | null; p90: number | null; p99: number | null; users: number };
  /** ISO weekday (1 = Monday ... 7 = Sunday) and hour, Tehran time. */
  heatmap: { dow: number; hour: number; bytes: number; conns: number }[];
  proxies: { kind: "mtproto" | "reality"; ref: string; bytes_up: number; bytes_down: number; conns_max: number }[];
  hourly: { at: string; bytes_up: number; bytes_down: number }[];
}

export interface TgGraph {
  x: number[];
  series: { key: string; name: string; type: string; values: number[] }[];
  percentage?: boolean;
  stacked?: boolean;
}

export interface ChannelStatsSnapshot {
  at: string;
  status: string;
  participants: number | null;
  period_start: string | null;
  period_end: string | null;
  followers: number | null;
  followers_prev: number | null;
  views_per_post: number | null;
  views_per_post_prev: number | null;
  shares_per_post: number | null;
  shares_per_post_prev: number | null;
  reactions_per_post: number | null;
  reactions_per_post_prev: number | null;
  notifications_pct: number | null;
  graphs: Partial<Record<"followers" | "growth" | "views_by_source" | "new_followers_by_source" | "languages" | "top_hours" | "interactions", TgGraph>>;
}

export interface PostViewsRow {
  message_id: number;
  views: number;
  forwards: number;
  reactions: number | null;
  at: string;
  posted_at: string | null;
  kind: string | null;
}

export interface ChannelStatsAnswer {
  main: ChannelStatsSnapshot | null;
  test: ChannelStatsSnapshot | null;
  posts: PostViewsRow[];
}

export interface TrackingLink {
  id: number;
  name: string;
  purpose: string;
  invite_link: string;
  created_at: string;
  renamed_at: string | null;
  joins: number;
  leaves: number;
  net: number;
  joins_7d: number;
  last_join_at: string | null;
}

// --- routes ---------------------------------------------------------------------------------------

const opts = (signal?: AbortSignal) => (signal ? { signal } : {});

export const getAnalyticsSummary = (signal?: AbortSignal) =>
  request<AnalyticsSummary>("GET", "/api/analytics/summary", undefined, opts(signal));

export const getAnalyticsSeries = (days: number, signal?: AbortSignal) =>
  request<AnalyticsSeries>("GET", `/api/analytics/series?days=${String(days)}`, undefined, opts(signal));

export const getAnalyticsUsage = (days: number, signal?: AbortSignal) =>
  request<AnalyticsUsage>("GET", `/api/analytics/usage?days=${String(days)}`, undefined, opts(signal));

export const getChannelStats = (signal?: AbortSignal) =>
  request<ChannelStatsAnswer>("GET", "/api/analytics/channel-stats", undefined, opts(signal));

export const getTrackingLinks = (signal?: AbortSignal) =>
  request<TrackingLink[]>("GET", "/api/analytics/links", undefined, opts(signal));

export const createTrackingLink = (name: string, purpose: string) =>
  request<TrackingLink>("POST", "/api/analytics/links", { name, purpose });

export const renameTrackingLink = (id: number, name: string, purpose: string) =>
  request<TrackingLink>("PUT", `/api/analytics/links/${String(id)}`, { name, purpose });

export const getAnalyticsSettings = (signal?: AbortSignal) =>
  request<Thresholds>("GET", "/api/analytics/settings", undefined, opts(signal));

export const putAnalyticsSettings = (s: Thresholds) => request<Thresholds>("PUT", "/api/analytics/settings", s);

// --- hooks ------------------------------------------------------------------------------------------

export const analyticsKeys = {
  all: ["analytics"] as const,
  summary: ["analytics", "summary"] as const,
  series: (days: number) => ["analytics", "series", days] as const,
  usage: (days: number) => ["analytics", "usage", days] as const,
  channelStats: ["analytics", "channel-stats"] as const,
  links: ["analytics", "links"] as const,
  settings: ["analytics", "settings"] as const,
};

function every(ms: number) {
  return (query: { state: { error: unknown } }) => (isEndpointMissing(query.state.error) ? false : ms);
}

export const useAnalyticsSummary = () =>
  useQuery({ queryKey: analyticsKeys.summary, queryFn: ({ signal }) => getAnalyticsSummary(signal), refetchInterval: every(60_000) });

export const useAnalyticsSeries = (days: number) =>
  useQuery({
    queryKey: analyticsKeys.series(days),
    queryFn: ({ signal }) => getAnalyticsSeries(days, signal),
    refetchInterval: every(120_000),
    placeholderData: (prev) => prev,
  });

export const useAnalyticsUsage = (days: number) =>
  useQuery({
    queryKey: analyticsKeys.usage(days),
    queryFn: ({ signal }) => getAnalyticsUsage(days, signal),
    refetchInterval: every(300_000),
    placeholderData: (prev) => prev,
  });

export const useChannelStats = () =>
  useQuery({ queryKey: analyticsKeys.channelStats, queryFn: ({ signal }) => getChannelStats(signal), refetchInterval: every(300_000) });

export const useTrackingLinks = () =>
  useQuery({ queryKey: analyticsKeys.links, queryFn: ({ signal }) => getTrackingLinks(signal), refetchInterval: every(60_000) });

/** The thresholds form edits a copy: background refetches must not overwrite what the owner types. */
export const useAnalyticsSettings = () =>
  useQuery({
    queryKey: analyticsKeys.settings,
    queryFn: ({ signal }) => getAnalyticsSettings(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
