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
  steps: Array<{ rate_eps: number; duration_s: number; reads: number; offsets?: number[] }>;
  total_reads: number;
  estimated_duration_s: number;
  read_timeout_s: number;
  max_outstanding_rule: string;
  rtt_source: string;
  caps: {
    max_rate_eps: number | null;
    max_run_seconds: number;
    max_total_reads: number;
  };
  stop_rules: Record<string, string | number>;
  watchdog: Record<string, string | number>;
  cooldown_seconds: number;
  warnings: string[];
  replay?: ReplayPlanInfo;
  environment: Record<string, string | null>;
  created_at: number;
}

export interface ReplayPredicted {
  peak_1s_eps: number;
  verdict: string;
  limits: ScenarioLimits | null;
}

export interface ReplayPlanInfo {
  source: Record<string, unknown> & { kind: string };
  variant: string;
  window_seconds: number;
  requested_peak_1s_eps: number;
  predicted: ReplayPredicted | null;
}

export interface ReplayResult extends ReplayPlanInfo {
  achieved: {
    sent: number;
    timeouts: number;
    delivery_failed: number;
    peak_1s_eps: number;
    first_to_last_s: number;
    wire_p50_ms: number | null;
    wire_p95_ms: number | null;
    wire_samples: number;
    echo_p50_ms: number | null;
    echo_p95_ms: number | null;
    rtt_source: string | null;
  } | null;
  shape_reproduced: boolean;
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
  verdict: string | null;
  ambient: {
    commands: number;
    state_reports: number;
    commands_per_s: number;
    state_per_s: number;
  } | null;
  abort_reason: string | null;
  environment: Record<string, string | null>;
  rtt_source: string | null;
  batch_id: string | null;
  mode: string;
  replay: ReplayResult | null;
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

export interface EnvelopeBurst {
  at: number;
  commands: number;
  duration_s: number;
  peak_eps: number;
}

export interface EnvelopeInstance {
  coverage: "wire" | "commands" | "none";
  provenance: string | null;
  peak: { eps_1s: number; at: number; eps_10s: number } | null;
  top_bursts: Array<{ at: number; eps_1s: number }>;
  benchmark_windows_excluded: number;
  limits: {
    sustained_eps?: number;
    sustained_kind?: string;
    mode?: string;
    measured_at?: number;
    ceiling_eps?: number;
  } | null;
  burst_utilization_pct: number | null;
  commanders: Array<{ commander: string; bursts: number; worst: EnvelopeBurst }>;
  composed_worst: { eps: number; commanders: string[]; observed_at: number } | null;
}

export interface EnvelopeView {
  window_seconds: number;
  instances: Record<string, EnvelopeInstance>;
  fanouts: Array<{
    commander: string;
    combined_eps: number;
    observed_at: number;
    instances: Record<string, number>;
  }>;
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
  trend: number | null;
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
  trend: number | null;
  provenance: string;
}

export interface LedgerInstanceRollup {
  total_us: number;
  us_per_s: number;
  pct_of_budget: number;
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
  instances: Record<string, LedgerInstanceRollup>;
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

export interface RecommendationSaving {
  us_per_s?: number;
  pct_of_budget?: number;
  p95_ms?: number;
  basis?: string;
  provenance?: string;
}

/** How much a saving is worth given how contended its budget actually is
 * (`recommend/significance.py`). Every field is optional: a detector that has
 * not been taught to assess significance serves an empty object, and the view
 * shows nothing rather than inventing a band. */
export interface RecommendationSignificance {
  band?: "high" | "moderate" | "low" | "unknown";
  denominator?: string;
  utilization_pct?: number | null;
  relief_pct?: number | null;
  /** A ready-to-display sentence body, lowercase and without a full stop. */
  rationale?: string;
}

/** Fields every cost block carries, whatever its kind (`recommend/cost.py`). */
interface RecommendationCostCommon {
  /** Which budget pays. Null when the trade was assessed and found free. */
  denominator?: string | null;
  /** Whether the action adds load. Drives how a cost reads, not whether it
   * is shown: a cost paid in delay is still a cost. */
  raises_load?: boolean;
  /** Plain-language sentence body authored by the detector. Contract field:
   * it is what a consumer renders when it does not recognize the kind. */
  note?: string;
}

/** The action sends more (or fewer) commands. */
export interface CostPublishDelta extends RecommendationCostCommon {
  kind: "publish_delta";
  publishes_before?: number;
  publishes_after?: number;
  publish_multiplier?: number | null;
  delta_commands_per_day?: number | null;
  delta_eps_mean?: number | null;
  measured_peak_eps?: number | null;
  capacity_limit_eps?: number | null;
}

/** The relief is bought on another coordinator: the one kind whose cost lands
 * on a different instance than its saving, so it names that instance. */
export interface CostDestinationLoad extends RecommendationCostCommon {
  kind: "destination_load";
  /** The coordinator that RECEIVES the load, which is never the one the
   * recommendation is filed under: this is the only kind whose cost and
   * saving land on different instances. */
  destination_instance?: string;
  peak_eps_before?: number;
  peak_eps_after?: number;
  capacity_limit_eps?: number | null;
  ceiling_eps?: number | null;
  peak_pct_of_limit_after?: number | null;
  verdict_after?: string;
  steady_us_per_s_delta?: number;
  fleet_steady_delta_us_per_s?: number;
}

/** The same commands, spread out: paid in wall clock, not in traffic.
 *
 * Deliberately carries no before/after publish counts: this is the one kind
 * that by construction moves no commands, so borrowing publish_delta's fields
 * would put a row of zeros on every finding. `commands_in_burst` is how many
 * commands the burst contains, not a delta. */
export interface CostCompletionDelay extends RecommendationCostCommon {
  kind: "completion_delay";
  commands_in_burst?: number;
  added_completion_ms?: number;
}

/** Fewer reports: paid in how late everything else hears about a change. */
export interface CostStaleness extends RecommendationCostCommon {
  kind: "staleness";
  reports_per_day_now?: number;
  reports_per_day_at_reference?: number;
  mean_interval_s_now?: number | null;
  mean_interval_s_at_reference?: number | null;
  added_delay_s?: number | null;
  presence_hardware?: boolean;
}

/** Assessed, and free. Deliberately distinct from an absent cost block, which
 * means nobody worked the trade out. */
export interface CostNone extends RecommendationCostCommon {
  kind: "none";
}

/** No kind this build can read: either no detector priced the trade (the API
 * serves `{}`), or the block came from a newer collector. Both render from
 * `note` alone, which is why `note` is a contract field.
 *
 * `kind` is typed as absent rather than `string` so the union keeps
 * discriminating. An unrecognized kind arriving over the wire falls through
 * the same default branch at runtime, which is what this member describes. */
export interface CostUnassessed extends RecommendationCostCommon {
  kind?: undefined;
}

/** What a change costs on the budgets it does not save on (`recommend/cost.py`).
 *
 * The shapes are not commensurable (commands, wall clock, staleness, a peak on
 * another coordinator, nothing at all), so the backend tags each with a `kind`
 * discriminator instead of serving one flat bag of optional fields. Consumers
 * dispatch on `kind`; they never sniff which keys happen to be present. */
export type RecommendationCost =
  | CostPublishDelta
  | CostDestinationLoad
  | CostCompletionDelay
  | CostStaleness
  | CostNone
  | CostUnassessed;

export interface RecommendationVerification {
  verdict: string;
  metric?: string;
  unit?: string;
  before_us_per_day?: number;
  after_us_per_day?: number;
  before_peak_eps?: number;
  after_peak_eps?: number;
  sustained_limit_eps?: number;
  paced_target_eps?: number;
  before_days?: number;
  after_days?: number;
  needs_days?: number;
  ratio?: number;
  basis?: string;
  note?: string;
  finalized?: boolean;
  checked_at: number;
}

export interface Recommendation {
  id: string;
  detector: string;
  instance: string;
  subject: string;
  finding: string;
  action: Record<string, unknown>;
  saving: RecommendationSaving;
  significance: RecommendationSignificance;
  cost: RecommendationCost;
  confidence: "high" | "medium" | "low";
  evidence: Array<Record<string, unknown>>;
  state: string;
  state_note: string | null;
  verification: RecommendationVerification | null;
  created_at: number;
  updated_at: number;
  state_changed_at: number | null;
}

export interface RecommendationRunDetector {
  findings?: number;
  inserted?: number;
  updated?: number;
  reopened?: number;
  deleted?: number;
  held?: number;
  error?: string;
}

export interface RecommendationRunStatus {
  last_run_at: number | null;
  next_run_due: number;
  detectors: string[];
  last_result: {
    ran_at: number;
    duration_ms: number;
    detectors: Record<string, RecommendationRunDetector>;
  } | null;
}

export interface RecommendationsView {
  recommendations: Recommendation[];
  counts: {
    by_state: Record<string, number>;
    open_by_instance: Record<string, number>;
  };
  run: RecommendationRunStatus;
}

export interface ScenarioMove {
  kind: "device" | "group";
  subject: string;
  from_instance: string;
  to_instance: string;
  group_resolution?: "unicasts" | "new_group";
}

export interface ScenarioLimits {
  sustained_eps?: number;
  sustained_kind?: string;
  mode?: string;
  measured_at?: number;
  stale_environment?: boolean;
  ceiling_eps?: number;
}

export interface ScenarioPeak {
  eps_1s: number;
  at: number;
}

export interface ScenarioContextDevice {
  name: string;
  ieee: string | null;
  router: boolean;
  us_per_s: number;
  groups: string[];
}

export interface ScenarioContextGroup {
  name: string;
  id: number | null;
  members: string[];
  us_per_s: number;
}

export interface ScenarioContextInstance {
  channel: number | null;
  steady: { us_per_s: number; pct_of_budget: number; provenance: string };
  burst: { peak_1s: ScenarioPeak | null; verdict: string; provenance: string };
  limits: ScenarioLimits | null;
  census: { routers: number };
  devices: ScenarioContextDevice[];
  groups: ScenarioContextGroup[];
}

export interface ScenarioContext {
  window_seconds: number;
  basis: { note: string };
  instances: Record<string, ScenarioContextInstance>;
}

export interface ScenarioMoveReport {
  kind: string;
  subject: string;
  from_instance: string;
  to_instance: string;
  router?: boolean;
  via_group?: string | null;
  members?: string[];
  commands: {
    chains_per_s: number;
    before_us_per_s: number;
    after_us_per_s: number;
    provenance: string;
    grouped: boolean;
  };
  reports?: { publishes_per_s: number; us_per_s: number; note: string };
  radio?: {
    status: string;
    best_observed_link_lqi: number | null;
    destination_channel: number | null;
    provenance: string;
  };
}

export interface ScenarioSplit {
  group: string;
  instance: string;
  to_instance: string;
  movers: string[];
  stayers: number;
  applied_resolution: string;
  added_us_per_s: Record<string, number>;
  note: string;
  provenance: string;
}

export interface ScenarioInstanceReport {
  steady: {
    before_us_per_s: number;
    after_us_per_s: number;
    before_pct_of_budget: number;
    after_pct_of_budget: number;
    second_order_us_per_s: number | null;
    provenance: string;
  };
  census: { routers_before: number; routers_after: number };
  burst: {
    before_peak_1s: ScenarioPeak | null;
    after_peak_1s: ScenarioPeak | null;
    before_peak_10s_eps: number | null;
    after_peak_10s_eps: number | null;
    wire_before_peak_1s: ScenarioPeak | null;
    verdict: string;
    provenance: string;
  };
  limits: ScenarioLimits | null;
  touched: boolean;
}

export interface ScenarioPriceReport {
  window_seconds: number;
  basis: { chains_window_seconds: number; ledger_days: string[]; note: string };
  moves: ScenarioMoveReport[];
  splits: ScenarioSplit[];
  instances: Record<string, ScenarioInstanceReport>;
  channel_pools: Array<{
    channel: number;
    instances: string[];
    combined_after_us_per_s: number;
    combined_after_pct_of_budget: number;
  }>;
}

export interface AdvisorScore {
  accepted: boolean;
  pressured_before: string[];
  instances: Record<
    string,
    { before_verdict: string; after_verdict: string; touched: boolean }
  >;
  notes: string[];
}

export interface ScenarioScoreReport extends ScenarioPriceReport {
  advisor: AdvisorScore;
}

export interface SavedScenario {
  name: string;
  moves: ScenarioMove[];
  saved_at: number;
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
