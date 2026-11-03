// One typed function per API route of the contract. Paths and bodies follow it exactly.

import { request } from "./api";
import type {
  Account,
  AuditEvent,
  FleetPlan,
  FleetSettings,
  KillSwitch,
  LoginResult,
  Me,
  Overview,
  RecoveryCodes,
  SessionInfo,
  SetupBegin,
  SetupStatus,
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
