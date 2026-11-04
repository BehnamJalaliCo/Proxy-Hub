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

// --- round 2: Telegram (team tg) --------------------------------------------------------------

export type ChannelRole = "main" | "test";

export interface TgChannel {
  username: string;
  title: string;
  can_post: boolean;
  can_edit: boolean;
  /** Must be false: the bot is never allowed to delete posts (CLAUDE.md rule 6). */
  can_delete: boolean;
  checked_at: string;
}

export interface TgPairing {
  code: string;
  expires_at: string;
}

export interface TgStatus {
  bot: { username: string; id: number } | null;
  owner_paired: boolean;
  pairing: TgPairing | null;
  channels: { main: TgChannel | null; test: TgChannel | null };
  outbox: { pending: number; failed: number };
  last_alert_at: string | null;
  /** Documented addition of the tg team (not in the round-2 contract): null, "bot_unauthorized" or
   * "updates_conflict". Optional: the page works the same without it. */
  bot_error?: string | null;
}

/** Asia/Tehran local times "HH:MM"; a window may cross midnight (end < start). */
export interface QuietWindow {
  start: string;
  end: string;
}

export interface TgSettings {
  quiet_windows: QuietWindow[];
  post_interval_min: number;
}

// --- the operator account for @MTProxybot (D37: a SECOND Telegram account) -------------------------

export type MtpState = "none" | "code_sent" | "password_needed" | "connected" | "disconnected" | "error";

/** GET /api/mtproxy/account. Never a secret and never the full phone number. */
export interface MtpAccount {
  state: MtpState;
  /** "+44•••••23": the first two and the last two digits only. */
  phone_masked: string | null;
  account: { first_name: string; user_id: number } | null;
  /** While a login waits for its code or two-step password. */
  login: { code_via: string | null; code_length: number | null; code_sent_at: string | null; expires_at: string | null } | null;
  /** The account could resolve @MTProxybot (null when there is no session). */
  mtproxybot_ok: boolean | null;
  connected_at: string | null;
  last_check_at: string | null;
  last_check_error: string | null;
  disconnected_at: string | null;
  disconnect_reason: string | null;
  error: string | null;
  /** "login_expired": the last login ran out of time. */
  notice: string | null;
  /** While ON, the automatic session check is paused. */
  kill_switch: boolean;
  check_interval_min: number;
}

export interface OutboxQueued {
  outbox_id: number;
}

// --- round 2: nodes (team live) ---------------------------------------------------------------

export type NodeState =
  | "requested"
  | "creating"
  | "booting"
  | "registering"
  | "ready"
  | "unhealthy"
  | "repairing"
  | "burned"
  | "draining"
  | "retiring"
  | "deleted";

export interface NodeAgent {
  registered: boolean;
  last_seen_at: string | null;
  telemt_version: string | null;
  /** Total open connections the node reports (a count, never who). */
  connections: number | null;
}

export interface NodeInfo {
  id: string;
  name: string;
  slot: number | null;
  location: string;
  server_type: string;
  /** A NodeState; typed wide so a new state on the server is shown, not hidden. */
  state: string;
  is_test: boolean;
  /** The node's own server address (ours, never a user's). */
  ipv4: string | null;
  created_at: string;
  ready_at: string | null;
  agent: NodeAgent;
}

export interface NodeEventItem {
  at: string;
  kind: string;
  detail: Record<string, unknown>;
}

export interface TestNodeRequested {
  node_id: string;
}

// --- round 2: Cloudflare and probes (team net) --------------------------------------------------

/** "deleted": the panel deleted the filtered zone from Cloudflare (terminal; never chosen by the owner). */
export type PoolState = "none" | "active" | "standby" | "filtered" | "retired" | "deleted";
/** The states the owner can choose in the dashboard. */
export type OwnerPoolState = Exclude<PoolState, "deleted">;

export type DomainVerdict = "filtered" | "clear" | "unknown";

/** What one measurement network (an operator/ASN in Iran) answered; never a person. */
export interface DomainCheckNetwork {
  network: string;
  asn: number | null;
  answered: number;
  hijacked: number;
}

/** The latest filter check of a domain from Iran (Globalping DNS; 10.10.34.x = the filter's answer). */
export interface DomainCheck {
  at: string;
  /** ok | no_probes | rate_limited | error | timeout */
  status: string;
  /** The domain's verdict right after this check (needs two strong rounds for "filtered"). */
  verdict: DomainVerdict;
  reasons: string[];
  probes: number;
  answered: number;
  hijacked: number;
  networks_answered: number;
  networks_hijacked: number;
  networks: DomainCheckNetwork[];
}

export interface CfZone {
  id: string;
  name: string;
  status: string;
  pool_state: PoolState;
  dns_edit?: boolean | null;
  /** When it was taken out of rotation (the deletion grace period starts here). */
  filtered_at?: string | null;
  /** When a filtered zone becomes deletable from Cloudflare. */
  delete_after?: string | null;
  /** Cloudflare refused the deletion for good: zone_edit_denied | cf_refused[:codes]. */
  delete_blocked?: string | null;
  /** Conditions a filtered zone still waits for (kill_switch_on, records_remaining, ...). */
  delete_waiting?: string[];
  deleted_at?: string | null;
  last_check?: DomainCheck | null;
}

export interface CfPoolSummary {
  standby_usable: number;
  /** An active or a filtered domain exists (so the "no standby left" warning makes sense). */
  in_use: boolean;
  kill_switch: boolean;
  delete_grace_hours: number;
  standby_check_hours: number;
}

export interface CfRecord {
  name: string;
  type: string;
  content: string;
  ttl: number;
  proxied: boolean;
  managed: true;
  updated_at: string | null;
}

export interface CfStatus {
  token_set: boolean;
  verified: boolean;
  checked_at: string | null;
  zones: CfZone[];
  /** Missing on a panel older than the automatic pool (D35). */
  pool?: CfPoolSummary;
  records: CfRecord[];
}

export type ProbeVerdict = "ok" | "suspect" | "blocked" | "unknown";

export interface ProbeSource {
  name: "globalping" | "check-host";
  ok: boolean;
  note: string | null;
  /** False until the source was used once (then "not measured yet", not "unreachable"). */
  checked?: boolean;
}

/** One measurement network (an operator/ASN in Iran), never a person. */
export interface ProbeNetwork {
  network: string;
  asn: number | null;
  ok: number;
  fail: number;
  at: string;
}

export interface ProbeTarget {
  node_id: string;
  name: string;
  ipv4: string;
  verdict: ProbeVerdict;
  networks: ProbeNetwork[];
}

export interface ProbeSummary {
  last_run_at: string | null;
  sources: ProbeSource[];
  targets: ProbeTarget[];
}
