import { useEffect, useRef, useState } from "react";
import {
  AirtimeLive,
  FleetMessage,
  HeadroomInstance,
  HeadroomView,
  InstanceInfo,
  LatencyStats,
  ProbeStats,
  WireLatencyStats,
  api,
  fleetSocketUrl,
} from "../api";

const KIND_ORDER = ["command", "state", "bridge", "availability", "probe", "other"];
const HISTORY_LENGTH = 60;

function totalPerSecond(kinds: Record<string, number> | undefined): number {
  if (!kinds) return 0;
  return Object.entries(kinds)
    .filter(([kind]) => kind !== "total_60s")
    .reduce((sum, [, count]) => sum + count, 0);
}

function Sparkline({ values }: { values: number[] }) {
  const width = 140;
  const height = 30;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((value, index) => `${(index * step).toFixed(1)},${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`)
    .join(" ");
  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      {values.length > 1 && <polyline points={points} />}
    </svg>
  );
}

interface Coverage {
  t0: boolean;
  t1: boolean;
  t2: boolean;
}

function CoverageMeter({ coverage }: { coverage: Coverage }) {
  const tiers: Array<[label: string, live: boolean, title: string]> = [
    ["T0", coverage.t0, "MQTT firehose (broker connection)"],
    ["T1", coverage.t1, "Z2M extension probe (heartbeating)"],
    ["T2", coverage.t2, "Passive wire tap (flow streaming)"],
  ];
  return (
    <span className="coverage">
      {tiers.map(([label, live, title]) => (
        <span key={label} className={live ? "chip ok" : "chip"} title={title}>
          {label} {live ? "✓" : "—"}
        </span>
      ))}
    </span>
  );
}

interface InstanceCardProps {
  instance: InstanceInfo;
  kinds: Record<string, number> | undefined;
  history: number[];
  latency?: LatencyStats;
  probe?: ProbeStats;
  airtime?: AirtimeLive;
  wireLatency?: WireLatencyStats;
  headroom?: HeadroomInstance;
  coverage: Coverage;
}

function InstanceCard({
  instance,
  kinds,
  history,
  latency,
  probe,
  airtime,
  wireLatency,
  headroom,
  coverage,
}: InstanceCardProps) {
  const online = instance.online;
  return (
    <div className="instance-card">
      <div className="instance-head">
        <span className={online === false ? "dot off" : online ? "dot on" : "dot unknown"} />
        <span className="instance-name">{instance.base_topic}</span>
        <span className="rate-big">
          {totalPerSecond(kinds)}
          <span className="rate-unit">msg/s</span>
        </span>
      </div>
      <Sparkline values={history} />
      <div className="kv-grid">
        <span>Z2M</span>
        <span>{instance.version ?? "—"}</span>
        <span>Channel</span>
        <span>{instance.channel ?? "—"}</span>
        <span>Adapter</span>
        <span className="clip" title={instance.adapter_port ?? undefined}>
          {instance.coordinator_type ?? "—"}
          {instance.adapter_port ? ` · ${instance.adapter_port}` : ""}
        </span>
        <span>Devices</span>
        <span>
          {instance.device_count} ({instance.router_count} routers,{" "}
          {instance.end_device_count} end devices)
        </span>
        <span>Groups</span>
        <span>{instance.group_count}</span>
        <span>Probe</span>
        <span>
          {probe?.version
            ? `v${probe.version}${probe.enabled === false ? " (paused)" : ""}`
            : "not deployed"}
        </span>
        <span>Airtime</span>
        <span title="Share of the CSMA-discounted channel budget, trailing 60 s (wire tier)">
          {airtime
            ? `${airtime.budget_pct_60s.toFixed(2)}% of budget · ${Math.round(airtime.us_per_s_60s)} µs/s`
            : "—"}
        </span>
        <span>Wire RTT</span>
        <span title="sendUnicast → delivery confirmation at the NCP boundary (T2, authoritative)">
          {wireLatency
            ? `p50 ${wireLatency.p50_ms} ms · p95 ${wireLatency.p95_ms} ms (${wireLatency.count})`
            : "—"}
        </span>
        <span>Knee</span>
        <span title="Calibrated sustainable command rate (§11); ≥ marks a lower bound. Load share from the trailing hour.">
          {headroom?.knee
            ? `${headroom.knee.kind === "mesh_knee" ? "" : "≥"}${headroom.knee.eps}/s` +
              (headroom.headroom
                ? ` · load ${headroom.headroom.knee_utilization_pct}% of knee`
                : "")
            : "not calibrated"}
        </span>
        <span>Z2M echo</span>
        <span title="Command → state-echo proxy at the Z2M boundary (T1, approximate)">
          {latency
            ? `p50 ${latency.p50_ms} ms · p95 ${latency.p95_ms} ms (${latency.count})`
            : "—"}
        </span>
      </div>
      <CoverageMeter coverage={coverage} />
      <div className="kinds">
        {KIND_ORDER.map((kind) => (
          <span key={kind} className="kind">
            <span className="kind-name">{kind}</span>
            <span className="kind-count">{kinds?.[kind] ?? 0}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

interface FleetProps {
  onReconfigure: () => void;
}

export default function Fleet({ onReconfigure }: FleetProps) {
  const [message, setMessage] = useState<FleetMessage | null>(null);
  const [socketState, setSocketState] = useState<"connecting" | "open" | "closed">("connecting");
  const historyRef = useRef<Record<string, number[]>>({});

  const [headroom, setHeadroom] = useState<HeadroomView | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api<HeadroomView>("/api/headroom?seconds=3600");
        if (alive) setHeadroom(data);
      } catch {
        // knee line simply stays absent until the next poll succeeds
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 30000);
    return () => {
      alive = false;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    let socket: WebSocket | null = null;
    let retry: number | undefined;

    function connect() {
      if (!alive) return;
      setSocketState("connecting");
      socket = new WebSocket(fleetSocketUrl());
      socket.onopen = () => alive && setSocketState("open");
      socket.onmessage = (event) => {
        if (!alive) return;
        const parsed = JSON.parse(event.data as string) as FleetMessage;
        const keys = [...parsed.instances.map((instance) => instance.base_topic), "*"];
        for (const key of keys) {
          const history = historyRef.current[key] ?? [];
          history.push(totalPerSecond(parsed.rates[key]));
          historyRef.current[key] = history.slice(-HISTORY_LENGTH);
        }
        setMessage(parsed);
      };
      socket.onclose = () => {
        if (!alive) return;
        setSocketState("closed");
        retry = window.setTimeout(connect, 2000);
      };
    }

    connect();
    return () => {
      alive = false;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  const broker = message?.broker;
  const instances = message?.instances ?? [];
  const globalRate = totalPerSecond(message?.rates["*"]);

  return (
    <>
      <div className={`banner ${broker?.state === "connected" ? "ok" : "warn"}`}>
        <span>
          Broker: <strong>{broker?.state ?? socketState}</strong>
          {broker?.error ? ` — ${broker.error}` : ""}
          {broker?.state === "connected" ? ` · ${globalRate} msg/s across the broker` : ""}
        </span>
        <button className="ghost small" onClick={onReconfigure}>
          Reconfigure
        </button>
      </div>

      {instances.length === 0 ? (
        <div className="panel">
          <p className="panel-kicker">Waiting for discovery</p>
          <p className="hint">
            Connected instances announce themselves on retained <code>&lt;base&gt;/bridge/info</code>{" "}
            topics, which arrive as soon as the subscription is up. If nothing appears within a few
            seconds, the account may lack read access to the Zigbee2MQTT base topics.
          </p>
        </div>
      ) : (
        <div className="cards">
          {instances.map((instance) => {
            const base = instance.base_topic;
            const probe = message?.probes[base];
            const heartbeat = probe?.last_heartbeat_at;
            const flow = message?.tap.flows.find((candidate) => candidate.instance === base);
            const now = message?.ts ?? 0;
            return (
              <InstanceCard
                key={base}
                instance={instance}
                kinds={message?.rates[base]}
                history={historyRef.current[base] ?? []}
                latency={message?.latency[base]}
                probe={probe}
                airtime={message?.tap.airtime[base]}
                wireLatency={message?.tap.latency[base]}
                headroom={headroom?.instances[base]}
                coverage={{
                  t0: broker?.state === "connected",
                  t1: heartbeat != null && now - heartbeat < 120,
                  t2: flow != null && now - flow.last_seen < 60,
                }}
              />
            );
          })}
        </div>
      )}
    </>
  );
}
