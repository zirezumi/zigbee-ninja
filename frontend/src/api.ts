export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export interface Health {
  status: string;
  version: string;
  setup_complete: boolean;
}

export interface Me {
  username: string;
}

export interface BrokerStatus {
  state: string;
  error?: string | null;
  connected_since?: number | null;
}

export interface BrokerView {
  configured: boolean;
  host?: string;
  port?: number;
  username?: string | null;
  status: BrokerStatus;
}

export interface InstanceInfo {
  base_topic: string;
  online: boolean | null;
  version: string | null;
  channel: number | null;
  pan_id: number | null;
  adapter_port: string | null;
  coordinator_type: string | null;
  coordinator_ieee: string | null;
  coordinator_revision: string | null;
  device_count: number;
  router_count: number;
  end_device_count: number;
  group_count: number;
  last_info_at: number | null;
}

export type RatesSnapshot = Record<string, Record<string, number>>;

export interface LatencyStats {
  count: number;
  p50_ms: number;
  p95_ms: number;
}

export interface ProbeStats {
  last_heartbeat_at: number | null;
  version: string | null;
  enabled: boolean | null;
  hooks: string[];
  counters: Record<string, number>;
  seq_gaps: number;
}

export interface WireStats {
  delivery_ok: number;
  delivery_failed: number;
  statuses: Record<string, number>;
  route_records: number;
  route_errors: Record<string, number>;
  loopbacks: number;
  layout_mismatch: number;
  incoming_trailing: Record<string, number>;
  lqi_ewma: number | null;
  rssi_ewma: number | null;
  pending_sends: number;
  counters_at: number | null;
  counters: Record<string, number> | null;
  counters_provenance: string;
}

export interface WireFlow {
  instance: string | null;
  coordinator: string;
  protocol_version: number | null;
  data_frames: number;
  ezsp_frames: Record<string, number>;
  to_coord_ash: Record<string, number>;
  from_coord_ash: Record<string, number>;
  crc_errors: number;
  retransmits: number;
  last_seen: number;
  wire: WireStats;
}

export interface AirtimeLive {
  buckets: Record<string, { airtime_us_60s: number; frames_60s: number }>;
  us_per_s_60s: number;
  airtime_pct_60s: number;
  budget_pct_60s: number;
  provenance: string;
}

export interface WireLatencyStats {
  count: number;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
}

export interface TapAgent {
  meta: Record<string, unknown>;
  connected_at: number;
  bytes: number;
  segments: number;
}

export interface TapStats {
  agents: number;
  agent_details: TapAgent[];
  flows: WireFlow[];
  airtime: Record<string, AirtimeLive>;
  latency: Record<string, WireLatencyStats>;
}

export interface AirtimeWindowInstance {
  buckets: Record<string, { airtime_us: number; frames: number }>;
  us_per_s: number;
  airtime_pct: number;
  budget_pct: number;
  provenance: string;
}

export interface AirtimeWindow {
  window_seconds: number;
  instances: Record<string, AirtimeWindowInstance>;
}

export interface TopologySummary {
  pulled_at: number;
  node_count: number;
  link_count: number;
  by_type: Record<string, number>;
  failed_nodes: string[];
  query_failures: Array<{ node: string; failed: string[] }>;
  unresponsive_nodes: string[];
  weak_links: Array<{ source: string; target: string; lqi: number }>;
  top_degree: Array<{ node: string; links: number }>;
}

export interface TopologyView {
  instances: Record<string, TopologySummary>;
}

export interface FleetMessage {
  ts: number;
  broker: BrokerStatus;
  instances: InstanceInfo[];
  rates: RatesSnapshot;
  latency: Record<string, LatencyStats>;
  probes: Record<string, ProbeStats>;
  tap: TapStats;
}

export interface TapView {
  token: string;
  stats: TapStats;
}

export interface HaView {
  configured: boolean;
  url?: string;
  status: {
    state: string;
    error?: string | null;
    counters?: Record<string, number>;
  };
}

export interface Tile {
  capability: string;
  target: string;
  status: string;
  version: string | null;
  bundled_version: string;
  health: string | null;
  drift: boolean;
  detail: string | null;
  probe: {
    version: string | null;
    hooks: string[];
    counters: Record<string, number>;
    seq_gaps: number;
    last_heartbeat_at: number | null;
  };
}

export interface CalibrationStep {
  rate_eps: number;
  duration_s: number;
  started_at: number;
  sent: number;
  completed: number;
  timeouts: number;
  deferred: number;
  achieved_eps: number;
  echo_p50_ms: number | null;
  echo_p95_ms: number | null;
  wire_p50_ms: number | null;
  wire_p95_ms: number | null;
  wire_samples: number;
  delivery_failed_delta: number;
  rtt_source: string | null;
  breach: string | null;
}

export interface CalibrationPlan {
  instance: string;
  target: string;
  target_ieee: string | null;
  get_attribute: string;
  topic: string;
  payload: string;
  traffic: string;
  steps: Array<{ rate_eps: number; duration_s: number; reads: number }>;
  total_reads: number;
  estimated_duration_s: number;
  read_timeout_s: number;
  max_outstanding_rule: string;
  rtt_source: string;
  caps: { max_rate_eps: number; max_run_seconds: number; max_total_reads: number };
  stop_rules: Record<string, string | number>;
  watchdog: Record<string, string | number>;
  cooldown_seconds: number;
  warnings: string[];
  environment: Record<string, string | null>;
  created_at: number;
}

export interface CalibrationPreview extends CalibrationPlan {
  authorization: string;
  authorization_expires_at: number;
}

export interface CalibrationActive {
  run_id: string;
  instance: string;
  target: string;
  state: string;
  started_at: number;
  step_index: number;
  total_steps: number;
  current: CalibrationStep | null;
  outstanding: number;
  sent_total: number;
  steps: CalibrationStep[];
  abort_requested: string | null;
  plan: CalibrationPlan;
}

export interface CalibrationKnee {
  eps: number;
  censored: boolean;
  breach: string | null;
  breach_rate_eps: number | null;
  rtt_source: string;
}

export interface CalibrationRecord {
  id: number;
  instance: string;
  target: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  knee_eps: number | null;
  steps: CalibrationStep[];
  knee: CalibrationKnee | null;
  abort_reason: string | null;
  environment: Record<string, string | null>;
  rtt_source: string | null;
}

export interface CalibrationView {
  active: CalibrationActive | null;
  cooldown_until: number | null;
  history: CalibrationRecord[];
}

export interface CalibrationCandidate {
  friendly_name: string;
  ieee_address: string | null;
  vendor: string | null;
  model: string | null;
  get_attribute: string | null;
  published_measurements: string[];
  binding_count: number;
  group_count: number;
  lqi: number | null;
  degree: number;
  eligible: boolean;
  reasons: string[];
  score: number | null;
}

export interface CandidatesView {
  instance: string;
  candidates: CalibrationCandidate[];
  topology_pulled_at: number | null;
}

export interface TopTarget {
  instance: string;
  target: string;
  commands: number;
  redundant: number;
  avg_first_echo_ms: number | null;
}

export interface AttributionSummary {
  window_seconds: number;
  classes: Record<string, Record<string, number>>;
  top_targets: TopTarget[];
  top_clients: { client: string; commands: number }[];
  totals: { chains: number; redundant: number; avg_first_echo_ms: number | null };
}

export interface RedundantRow {
  instance: string;
  target: string;
  count: number;
  client: string;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function fleetSocketUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/api/ws/fleet`;
}
