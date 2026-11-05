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
  source?: string;
  /** False when the network had no probe in that measurement. */
  probe?: boolean;
  /** "https": a real TLS handshake (what decides, P15); absent on TCP information. */
  kind?: "https";
  port?: number | null;
  /** Why a handshake failed: tls_timeout, tcp_timeout, reset, other. */
  failure?: string | null;
}

export interface ProbeTarget {
  node_id: string;
  name: string;
  ipv4: string;
  location?: string;
  verdict: ProbeVerdict;
  verdict_at?: string | null;
  verdict_reasons?: string[];
  /** TLS handshakes per network and port (443 and the MTProto port). */
  networks: ProbeNetwork[];
  /** The name (or IP) the handshakes went to: the node's own DNS name when it points at the node. */
  tls_target?: string | null;
  /** TCP pings: information only (MCI and Rightel answer a SYN themselves for blocked addresses). */
  tcp_networks?: ProbeNetwork[];
}

export type LocationClass = "good" | "mixed" | "unknown" | "bad";

export interface LocationNetwork {
  key: string;
  network: string;
  asn: number | null;
  state: "open" | "closed";
  at: string;
  nodes_open: number;
  nodes_closed: number;
  /** MCI or Rightel: the networks that decide. */
  key_network: boolean;
}

export interface LocationReach {
  location: string;
  /** In the owner's allowed list (fleet settings). */
  allowed: boolean;
  class: LocationClass;
  networks: LocationNetwork[];
}

export interface LocationsSummary {
  window_days: number;
  key_networks: string[];
  locations: LocationReach[];
}

export interface ProbeSummary {
  last_run_at: string | null;
  sources: ProbeSource[];
  targets: ProbeTarget[];
  /** Missing on a panel older than the location scores. */
  locations?: LocationsSummary;
}

// --- candidate REALITY targets / fake-TLS domains measured from Iran (P15) -----------------------

export interface TargetSettings {
  enabled: boolean;
  candidates: string[];
  interval_h: number;
}

export interface TargetNetwork {
  key: string;
  state: "up" | "down" | "nodata";
  network: string;
  asn: number | null;
  ms: number | null;
  key_network: boolean;
}

export interface TargetCandidate {
  host: string;
  /** In the owner's candidate list (else it is only a current target). */
  listed: boolean;
  current_for: ("reality" | "mtproto")[];
  measured_at: string | null;
  networks: TargetNetwork[];
  prev_measured_at: string | null;
  prev_networks: TargetNetwork[];
  /** Open from Iran: up on a key network and down on none, in the latest round. */
  open: boolean;
  /** Key networks it was closed on in the latest two rounds. */
  blocked_on: string[];
  ms: number | null;
  /** Our nodes' own TLS 1.3 + HTTP/2 check of it (REALITY), if any node checked it lately. */
  node_check: "passed" | "failed" | null;
  node_ok_at: string | null;
  node_fail_at: string | null;
}

export interface TargetsSummary {
  settings: TargetSettings;
  defaults: TargetSettings;
  key_networks: string[];
  reality: { mode: "auto" | "manual"; target: string; transport: string };
  mtproto: { mode: "auto" | "manual"; tls_domain: string } | null;
  last_full_at: string | null;
  next_full_at: string | null;
  candidates: TargetCandidate[];
}
