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

export interface FleetMessage {
  ts: number;
  broker: BrokerStatus;
  instances: InstanceInfo[];
  rates: RatesSnapshot;
  latency: Record<string, LatencyStats>;
  probes: Record<string, ProbeStats>;
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
