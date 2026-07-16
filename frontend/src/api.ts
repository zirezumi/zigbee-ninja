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
  avg_tx: number | null;
  avg_tx_samples: number;
  avg_tx_rejected: number;
  avg_tx_last: Record<string, number | boolean | string> | null;
  avg_tx_provenance: string;
  retry_rate: number | null;
  retry_rate_samples: number;
  retry_rate_last: Record<string, number> | null;
  retry_rate_provenance: string;
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

export interface TopologyGraphNode {
  id: string;
  name: string;
  type: string;
  failed: boolean;
  degree: number;
  routes_via: number;
}

export interface TopologyGraphLink {
  source: string;
  target: string;
  lqi: number | null;
}

export interface TopologyGraph {
  instance: string;
  pulled_at: number;
  nodes: TopologyGraphNode[];
  links: TopologyGraphLink[];
}

export interface AlertBrief {
  instance: string;
  severity: string | null;
  name: string | null;
}

export interface FusionView {
  state: string;
  matched_5m: number;
  wire_only_5m: number;
  probe_only_5m: number;
  clock_offset_ms: number | null;
  offset_samples: number;
  overflow_drops: number;
}

export interface FleetMessage {
  ts: number;
  broker: BrokerStatus;
  instances: InstanceInfo[];
  rates: RatesSnapshot;
  latency: Record<string, LatencyStats>;
  probes: Record<string, ProbeStats>;
  tap: TapStats;
  fusion: Record<string, FusionView>;
  alerts: AlertBrief[];
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
  mode: string; // single | spread
  instance: string;
  target: string;
  target_ieee: string | null;
  get_attribute: string | null;
  topic: string | null;
  payload: string | null;
  targets: Array<{
    friendly_name: string;
    get_attribute: string;
    topic: string;
    payload: string;
  }>;
  per_target_max_eps: number | null;
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
  mode: string;
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
  batch_id: string | null;
  mode: string;
}

export interface CalibrationBulkStatus {
  batch_id: string;
  position: number;
  total: number;
  state: string;
  started_at: number;
  runs: Array<{ instance: string; target: string }>;
  skipped: Array<{ instance: string; target?: string; reason: string }>;
  abort_requested: boolean;
}

export interface CalibrationBulkPreview {
  batch: boolean;
  batch_id: string;
  runs: CalibrationPlan[];
  skipped: Array<{ instance: string; target?: string; reason: string }>;
  total_reads: number;
  estimated_duration_s: number;
  cooldown_between_runs_s: number;
  created_at: number;
  authorization: string;
  authorization_expires_at: number;
}

export interface CalibrationView {
  active: CalibrationActive | null;
  bulk: CalibrationBulkStatus | null;
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

export interface HeadroomKnee {
  eps: number;
  kind: string;
  mode: string;
  breach: string | null;
  censored: boolean;
  rtt_source: string | null;
  target: string | null;
  measured_at: number | null;
  environment: Record<string, string | null>;
  stale_environment: boolean;
}

export interface HeadroomInstance {
  knee: HeadroomKnee | null;
  denominators: {
    channel_budget: { us_per_s: number; pct: number; provenance: string };
    ncp_knee: { eps: number; provenance: string } | null;
    pipeline: { eps: number; provenance: string } | null;
  };
  rates: {
    p50_eps: number;
    p95_eps: number;
    max_eps: number;
    windows: number;
  } | null;
  headroom: {
    steady_eps: number;
    burst_eps: number;
    knee_utilization_pct: number;
    granularity: string;
  } | null;
  scatter: Array<{ eps: number; p95_ms: number }>;
  benchmark_windows_excluded: number;
}

export interface HeadroomView {
  window_seconds: number;
  instances: Record<string, HeadroomInstance>;
}

export interface AlertMetricInfo {
  metric: string;
  scope: string;
  kind: string;
  unit: string;
  description: string;
}

export interface AlertRule {
  id: number;
  builtin: string | null;
  name: string;
  metric: string;
  instance: string;
  op: string;
  threshold: number;
  clear_threshold: number | null;
  sustain_seconds: number;
  severity: string;
  enabled: boolean;
  created_at?: string;
}

export interface ActiveAlert {
  event_id: number;
  rule_id: number;
  name: string | null;
  metric: string | null;
  unit: string | null;
  instance: string;
  severity: string | null;
  opened_at: number | null;
  value: number | null;
  peak: number | null;
  threshold: number | null;
  op: string | null;
}

export interface AlertsView {
  active: ActiveAlert[];
  rules: AlertRule[];
  metrics: AlertMetricInfo[];
}

export interface AlertEventContext {
  name?: string;
  metric?: string;
  op?: string;
  threshold?: number;
  severity?: string;
  value_at_open?: number;
  closed?: string;
}

export interface AlertEvent {
  id: number;
  rule_id: number;
  instance: string;
  opened_at: number;
  cleared_at: number | null;
  peak_value: number | null;
  context: AlertEventContext;
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
  top_clients: { client: string; commands: number; label?: string }[];
  totals: { chains: number; redundant: number; avg_first_echo_ms: number | null };
}

export interface RedundantRow {
  instance: string;
  target: string;
  count: number;
  client: string;
  label?: string;
}

export interface LedgerParams {
  n_routers: number;
  avg_tx: number;
  avg_tx_measured: boolean;
  retry_rate: number;
  retry_rate_measured: boolean;
}

export interface LedgerCommanderRow {
  instance: string;
  commander: string;
  chains: number;
  tx_us: number;
  rx_us: number;
  total_us: number;
  us_per_s: number;
  pct_of_budget: number;
  provenance: string;
  params: LedgerParams;
}

export interface LedgerDeviceRow {
  instance: string;
  device: string;
  publishes: number;
  autonomous_us: number;
  us_per_s: number;
  pct_of_budget: number;
  provenance: string;
}

export interface LedgerView {
  window_seconds: number;
  days: string[];
  effective_seconds: number;
  recording_since: number | null;
  commander_count: number;
  device_count: number;
  commanders: LedgerCommanderRow[];
  devices: LedgerDeviceRow[];
  totals: {
    chains: number;
    tx_us: number;
    rx_us: number;
    autonomous_publishes: number;
    autonomous_us: number;
    total_us: number;
    us_per_s: number;
    pct_of_budget: number;
  };
}

export interface JournalEntry {
  ts: number;
  instance: string;
  kind: string;
  subject: string;
  detail: Record<string, unknown>;
}

export interface JournalView {
  window_seconds: number;
  entries: JournalEntry[];
}

export interface RuntimeSettings {
  retention_rollup_days: number;
  retention_chains_hours: number;
  retention_topology_snapshots: number;
  raw_event_quota_mb: number;
  raw_event_horizon_hours: number;
  client_labels: Record<string, string>;
}

export interface BurstSourceBin {
  events: number;
  bytes: number;
}

export interface BurstBin {
  bin: number;
  mqtt?: BurstSourceBin;
  wire?: BurstSourceBin;
}

export interface BurstTimeline {
  start: number;
  end: number;
  bucket_ms: number;
  bins: BurstBin[];
  store: {
    buffered: number;
    dropped: number;
    hot_rows: number;
    segments: number;
    segment_bytes: number;
  };
}

export interface BurstEvent {
  ts: number;
  source: string;
  instance: string;
  kind: string;
  direction: string;
  target: string | null;
  size: number;
}

export interface BurstChain {
  target: string;
  verb: string;
  opened_at: number;
  client: string | null;
  echo_count: number;
  first_echo_ms: number | null;
  redundant: number;
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
