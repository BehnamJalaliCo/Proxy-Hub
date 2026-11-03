// Shapes of the panel API, exactly as the shared API contract defines them (auth, fleet and
// dashboard teams). Times are ISO-8601 UTC strings; money is a decimal string. Do not add fields
// here that the contract does not have.

/** Every error body: {"error": "<code>", "message_fa": "<Persian sentence>"} plus a few extras. */
export interface ApiErrorBody {
  error: string;
  message_fa?: string;
  /** 429 locked */
  retry_after_s?: number;
  /** 409 account_not_empty */
  foreign_servers?: number;
}

// --- auth -------------------------------------------------------------------------------------

export interface SetupStatus {
  setup_required: boolean;
}

export interface SetupBegin {
  enrollment_id: string;
  totp_uri: string;
  totp_secret: string;
}

export interface RecoveryCodes {
  recovery_codes: string[];
}

export interface LoginResult {
  username: string;
}

export interface Me {
  username: string;
  created_at: string;
  last_login_at: string | null;
}

export interface SessionInfo {
  id: string;
  created_at: string;
  last_seen_at: string;
  user_agent: string | null;
  current: boolean;
}

// --- fleet / accounts / settings --------------------------------------------------------------

export interface Overview {
  kill_switch: boolean;
  dry_run: boolean;
  budget_cap_eur: string | null;
  node_cap_total: number | null;
  target_slots: number;
  nodes_live: number;
  month_projected_eur: string;
  accounts: { active: number; locked: number; total: number };
  last_fleet_run_at: string | null;
  worker_heartbeat_age_s: number | null;
}

export type AccountState = "verifying" | "preparing" | "active" | "draining" | "retired" | "locked";

export interface Account {
  id: string;
  name: string;
  state: AccountState;
  state_reason: string | null;
  servers_in_account: number | null;
  learned_server_limit: number | null;
  created_at: string;
}

/** proxyhub/settings_schema.py FleetSettings; decimals as strings. */
export interface FleetSettings {
  kill_switch: boolean;
  dry_run: boolean;
  budget_cap_eur: string | null;
  node_cap_total: number | null;
  creations_per_hour_cap: number;
  target_slots: number;
  allowed_server_types: string[];
  locations: string[];
}

export interface KillSwitch {
  kill_switch: boolean;
}

export type PlanActionKind = "create" | "delete" | "none";

export interface PlanAction {
  action: PlanActionKind;
  target: string;
  allowed: boolean;
  reasons: string[];
}

export interface FleetPlan {
  generated_at: string | null;
  /** Always true in phase 1 (the contract says so); typed wide so a later change cannot mislead. */
  dry_run: boolean;
  actions: PlanAction[];
}

export interface AuditEvent {
  id: number;
  at: string;
  actor: string;
  action: string;
  target: string | null;
  dry_run: boolean;
  allowed: boolean | null;
  reasons: string[] | null;
  result: unknown;
}
