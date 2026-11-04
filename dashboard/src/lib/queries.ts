// TanStack Query hooks for read-only data. Calls that carry a secret (password, TOTP code, Hetzner,
// Telegram or Cloudflare token) do NOT go through useMutation: its cache would keep the input
// ("variables") in memory.

import { QueryClient, useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { isApiError, isEndpointMissing } from "./api";
import * as api from "./endpoints";
import type { AuditEvent } from "./types";

export const keys = {
  setupStatus: ["setup-status"] as const,
  me: ["me"] as const,
  sessions: ["sessions"] as const,
  overview: ["overview"] as const,
  plan: ["fleet-plan"] as const,
  accounts: ["accounts"] as const,
  settings: ["settings", "fleet"] as const,
  events: (scope: string) => ["events", scope] as const,
  telegram: ["telegram", "status"] as const,
  telegramSettings: ["telegram", "settings"] as const,
  nodes: ["nodes"] as const,
  nodeEvents: (id: string) => ["nodes", id, "events"] as const,
  cloudflare: ["cloudflare"] as const,
  probes: ["probes"] as const,
  mtproxyAccount: ["mtproxy", "account"] as const,
};

/** Poll every `ms`, but stop while the route does not exist on this panel (no 404 every 15 s). */
function pollUnlessMissing(ms: number) {
  return (query: { state: { error: unknown } }) => (isEndpointMissing(query.state.error) ? false : ms);
}

/** Statuses that will not change by retrying. */
const FINAL = new Set([400, 401, 403, 404, 409, 422, 429]);

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: true,
        retry: (count, error) => !(isApiError(error) && FINAL.has(error.status)) && count < 2,
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
      },
      mutations: { retry: false },
    },
  });
}

export const useSetupStatus = () =>
  useQuery({ queryKey: keys.setupStatus, queryFn: ({ signal }) => api.getSetupStatus(signal), staleTime: 30_000 });

export const useMe = () =>
  useQuery({ queryKey: keys.me, queryFn: ({ signal }) => api.getMe(signal), staleTime: 60_000 });

export const useSessions = () =>
  useQuery({ queryKey: keys.sessions, queryFn: ({ signal }) => api.getSessions(signal), refetchInterval: 60_000 });

export const useOverview = () =>
  useQuery({ queryKey: keys.overview, queryFn: ({ signal }) => api.getOverview(signal), refetchInterval: 15_000 });

export const useFleetPlan = () =>
  useQuery({ queryKey: keys.plan, queryFn: ({ signal }) => api.getFleetPlan(signal), refetchInterval: 30_000 });

export const useAccounts = () =>
  useQuery({ queryKey: keys.accounts, queryFn: ({ signal }) => api.getAccounts(signal), refetchInterval: 30_000 });

/** The settings form edits a copy: background refetches must not overwrite what the owner types. */
export const useFleetSettings = () =>
  useQuery({
    queryKey: keys.settings,
    queryFn: ({ signal }) => api.getFleetSettings(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

export const useRecentEvents = (limit: number) =>
  useQuery({
    queryKey: keys.events(`recent-${String(limit)}`),
    queryFn: ({ signal }) => api.getEvents(limit, undefined, signal),
    refetchInterval: 20_000,
  });

export const EVENTS_PAGE = 100;

export const useEventPages = () =>
  useInfiniteQuery({
    queryKey: keys.events("pages"),
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam, signal }) => api.getEvents(EVENTS_PAGE, pageParam, signal),
    getNextPageParam: (last: AuditEvent[]) => {
      if (last.length < EVENTS_PAGE) return undefined;
      const ids = last.map((e) => e.id);
      return Math.min(...ids);
    },
    refetchInterval: 30_000,
  });

// --- round 2 ---------------------------------------------------------------------------------------

/** While a pairing code waits for the owner's /start (one the server shows, or `createdHere`), the
 * page polls every 4 s so "connected" shows up soon after; otherwise every 20 s. */
export const useTelegramStatus = (createdHere = false) =>
  useQuery({
    queryKey: keys.telegram,
    queryFn: ({ signal }) => api.getTelegramStatus(signal),
    refetchInterval: (query) => {
      if (isEndpointMissing(query.state.error)) return false;
      const d = query.state.data;
      const waiting = d !== undefined && d.bot !== null && (d.pairing !== null || (createdHere && !d.owner_paired));
      return waiting ? 4_000 : 20_000;
    },
  });

/** The schedule editor edits a copy: background refetches must not overwrite what the owner types. */
export const useTelegramSettings = () =>
  useQuery({
    queryKey: keys.telegramSettings,
    queryFn: ({ signal }) => api.getTelegramSettings(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

export const useNodes = () =>
  useQuery({ queryKey: keys.nodes, queryFn: ({ signal }) => api.getNodes(signal), refetchInterval: pollUnlessMissing(15_000) });

export const useNodeEvents = (id: string | null) =>
  useQuery({
    queryKey: keys.nodeEvents(id ?? "-"),
    queryFn: ({ signal }) => api.getNodeEvents(id ?? "", signal),
    enabled: id !== null,
    refetchInterval: pollUnlessMissing(15_000),
  });

export const useCloudflareStatus = () =>
  useQuery({
    queryKey: keys.cloudflare,
    queryFn: ({ signal }) => api.getCloudflareStatus(signal),
    refetchInterval: pollUnlessMissing(60_000),
  });

export const useProbeSummary = () =>
  useQuery({
    queryKey: keys.probes,
    queryFn: ({ signal }) => api.getProbeSummary(signal),
    refetchInterval: pollUnlessMissing(60_000),
  });

/** The operator account: every 5 s while a login waits for its code or password (the countdown),
 * otherwise every 30 s. */
export const useMtproxyAccount = () =>
  useQuery({
    queryKey: keys.mtproxyAccount,
    queryFn: ({ signal }) => api.getMtproxyAccount(signal),
    refetchInterval: (query) => {
      if (isEndpointMissing(query.state.error)) return false;
      const st = query.state.data?.state;
      return st === "code_sent" || st === "password_needed" ? 5_000 : 30_000;
    },
  });
