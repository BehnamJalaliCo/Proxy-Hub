// One typed function per API route of the contract. Paths and bodies follow it exactly.

import { request } from "./api";
import type {
  Account,
  AuditEvent,
  CfStatus,
  ChannelRole,
  FleetPlan,
  FleetSettings,
  KillSwitch,
  LoginResult,
  Me,
  MtpAccount,
  NodeEventItem,
  NodeInfo,
  OutboxQueued,
  Overview,
  OwnerPoolState,
  ProbeSummary,
  RecoveryCodes,
  SessionInfo,
  SetupBegin,
  SetupStatus,
  TestNodeRequested,
  TgPairing,
  TgSettings,
  TgStatus,
} from "./types";

// --- setup and login (public routes; their 401/404 are answers, not a lost session) ----------

export const getSetupStatus = (signal?: AbortSignal) =>
  request<SetupStatus>("GET", "/api/setup/status", undefined, { noAuthRedirect: true, ...(signal ? { signal } : {}) });

export const setupBegin = (token: string) =>
  request<SetupBegin>("POST", "/api/setup/begin", { token }, { noAuthRedirect: true });

export interface SetupFinishInput {
  token: string;
  enrollment_id: string;
  username: string;
  password: string;
  totp_code: string;
}

export const setupFinish = (input: SetupFinishInput) =>
  request<RecoveryCodes>("POST", "/api/setup/finish", input, { noAuthRedirect: true });

export type LoginInput =
  | { username: string; password: string; totp_code: string }
  | { username: string; password: string; recovery_code: string };

export const login = (input: LoginInput) =>
  request<LoginResult>("POST", "/api/auth/login", input, { noAuthRedirect: true });

// --- the owner's own session ------------------------------------------------------------------

export const logout = () => request<undefined>("POST", "/api/auth/logout", undefined, { noAuthRedirect: true });

export const getMe = (signal?: AbortSignal) =>
  request<Me>("GET", "/api/auth/me", undefined, signal ? { signal } : {});

export const getSessions = (signal?: AbortSignal) =>
  request<SessionInfo[]>("GET", "/api/auth/sessions", undefined, signal ? { signal } : {});

export const revokeSession = (id: string) =>
  request<undefined>("DELETE", `/api/auth/sessions/${encodeURIComponent(id)}`);

export const regenerateRecoveryCodes = (totp_code: string) =>
  request<RecoveryCodes>("POST", "/api/auth/recovery-codes/regenerate", { totp_code });

// --- fleet, accounts, settings ------------------------------------------------------------------

export const getOverview = (signal?: AbortSignal) =>
  request<Overview>("GET", "/api/overview", undefined, signal ? { signal } : {});

export const getAccounts = (signal?: AbortSignal) =>
  request<Account[]>("GET", "/api/accounts", undefined, signal ? { signal } : {});

/** The token goes to the server once and is never shown, stored or echoed by the dashboard. */
export const addAccount = (name: string, token: string) =>
  request<Account>("POST", "/api/accounts", { name, token });

export const drainAccount = (id: string) =>
  request<Account>("POST", `/api/accounts/${encodeURIComponent(id)}/drain`);

export const retireAccount = (id: string) =>
  request<Account>("POST", `/api/accounts/${encodeURIComponent(id)}/retire`);

export const getFleetSettings = (signal?: AbortSignal) =>
  request<FleetSettings>("GET", "/api/settings/fleet", undefined, signal ? { signal } : {});

export const putFleetSettings = (settings: FleetSettings) =>
  request<FleetSettings>("PUT", "/api/settings/fleet", settings);

export const setKillSwitch = (on: boolean) => request<KillSwitch>("POST", "/api/kill-switch", { on });

export const getFleetPlan = (signal?: AbortSignal) =>
  request<FleetPlan>("GET", "/api/fleet/plan", undefined, signal ? { signal } : {});

export const getEvents = (limit: number, beforeId?: number, signal?: AbortSignal) => {
  const q = new URLSearchParams({ limit: String(limit) });
  if (beforeId !== undefined) q.set("before_id", String(beforeId));
  return request<AuditEvent[]>("GET", `/api/events?${q.toString()}`, undefined, signal ? { signal } : {});
};

// --- round 2: Telegram (team tg) ----------------------------------------------------------------

const opts = (signal?: AbortSignal) => (signal ? { signal } : {});

export const getTelegramStatus = (signal?: AbortSignal) =>
  request<TgStatus>("GET", "/api/telegram/status", undefined, opts(signal));

/** The bot token goes to the server once (checked there with getMe) and is never shown again. */
export const putTelegramBot = (token: string) => request<TgStatus>("PUT", "/api/telegram/bot", { token });

export const createTelegramPairing = () => request<TgPairing>("POST", "/api/telegram/pairing");

export const putTelegramChannel = (role: ChannelRole, username: string) =>
  request<TgStatus>("PUT", "/api/telegram/channel", { role, username });

export const getTelegramSettings = (signal?: AbortSignal) =>
  request<TgSettings>("GET", "/api/telegram/settings", undefined, opts(signal));

export const putTelegramSettings = (settings: TgSettings) =>
  request<TgSettings>("PUT", "/api/telegram/settings", settings);

export const sendTelegramTestAlert = () => request<Record<string, never>>("POST", "/api/telegram/test-alert");

/** Goes to the TEST channel only. */
export const sendTelegramTestPost = () => request<OutboxQueued>("POST", "/api/telegram/test-post");

// --- the operator account for @MTProxybot (D37) ------------------------------------------------------
// api_hash, code and password go to the server once each and are never kept by the dashboard.

export const getMtproxyAccount = (signal?: AbortSignal) =>
  request<MtpAccount>("GET", "/api/mtproxy/account", undefined, opts(signal));

export const startMtproxyLogin = (api_id: string, api_hash: string, phone: string) =>
  request<MtpAccount>("PUT", "/api/mtproxy/account", { api_id, api_hash, phone });

export const sendMtproxyCode = (code: string) => request<MtpAccount>("POST", "/api/mtproxy/account/code", { code });

export const sendMtproxyPassword = (password: string) =>
  request<MtpAccount>("POST", "/api/mtproxy/account/password", { password });

/** Ends the session on Telegram, empties the vault; also cancels a login in progress. */
export const logoutMtproxyAccount = () => request<MtpAccount>("POST", "/api/mtproxy/account/logout");

// --- round 2: nodes (team live) -----------------------------------------------------------------

export const getNodes = (signal?: AbortSignal) => request<NodeInfo[]>("GET", "/api/nodes", undefined, opts(signal));

/** No location: the server picks one from the fleet settings. */
export const requestTestNode = (location?: string) =>
  request<TestNodeRequested>("POST", "/api/nodes/test", location ? { location } : {});

export const retireNode = (id: string) =>
  request<Record<string, never>>("POST", `/api/nodes/${encodeURIComponent(id)}/retire`);

export const getNodeEvents = (id: string, signal?: AbortSignal) =>
  request<NodeEventItem[]>("GET", `/api/nodes/${encodeURIComponent(id)}/events`, undefined, opts(signal));

// --- round 2: Cloudflare and probes (team net) ----------------------------------------------------

export const getCloudflareStatus = (signal?: AbortSignal) =>
  request<CfStatus>("GET", "/api/cloudflare/status", undefined, opts(signal));

/** The Cloudflare token goes to the server once and is never shown again. */
export const putCloudflareToken = (token: string) => request<CfStatus>("PUT", "/api/cloudflare/token", { token });

/** "deleted" is never sent: only the panel itself deletes a filtered zone from Cloudflare. */
export const putCloudflarePool = (zone_id: string, pool_state: OwnerPoolState) =>
  request<CfStatus>("PUT", "/api/cloudflare/pool", { zone_id, pool_state });

export const getProbeSummary = (signal?: AbortSignal) =>
  request<ProbeSummary>("GET", "/api/probes/summary", undefined, opts(signal));

export const runProbe = (node_id: string) => request<Record<string, never>>("POST", "/api/probes/run", { node_id });
