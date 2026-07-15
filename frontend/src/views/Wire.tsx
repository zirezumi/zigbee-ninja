import { useCallback, useEffect, useState } from "react";
import {
  AirtimeLive,
  AirtimeWindow,
  api,
  FleetMessage,
  fleetSocketUrl,
  TapStats,
  WireFlow,
  WireLatencyStats,
} from "../api";

const WINDOWS: Array<[label: string, seconds: number]> = [
  ["15 min", 900],
  ["1 h", 3600],
  ["24 h", 86400],
];

const BUCKET_ORDER = ["tx_unicast", "tx_groupcast", "rx", "rx_mesh"];
const BUCKET_LABELS: Record<string, string> = {
  tx_unicast: "tx unicast",
  tx_groupcast: "tx groupcast",
  rx: "rx",
  rx_mesh: "rx mesh",
};

function AirtimeBar({ buckets }: { buckets: AirtimeLive["buckets"] }) {
  const total = BUCKET_ORDER.reduce(
    (sum, bucket) => sum + (buckets[bucket]?.airtime_us_60s ?? 0),
    0,
  );
  if (!total) return <div className="classbar empty" />;
  return (
    <div className="classbar">
      {BUCKET_ORDER.filter((bucket) => buckets[bucket]).map((bucket) => (
        <span
          key={bucket}
          className={`classbar-seg seg-${bucket}`}
          style={{ width: `${(buckets[bucket].airtime_us_60s / total) * 100}%` }}
          title={`${BUCKET_LABELS[bucket]}: ${(buckets[bucket].airtime_us_60s / 1000).toFixed(0)} ms over 60 s (${buckets[bucket].frames_60s} frames)`}
        />
      ))}
    </div>
  );
}

interface FlowCardProps {
  flow: WireFlow;
  airtime?: AirtimeLive;
  latency?: WireLatencyStats;
  now: number;
}

function FlowCard({ flow, airtime, latency, now }: FlowCardProps) {
  const wire = flow.wire;
  const fresh = now - flow.last_seen < 60;
  const ezspTop = Object.entries(flow.ezsp_frames)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 6);
  return (
    <div className="instance-card">
      <div className="instance-head">
        <span className={fresh ? "dot on" : "dot off"} />
        <span className="instance-name">{flow.instance ?? flow.coordinator}</span>
        <span className="rate-big">
          {airtime ? airtime.budget_pct_60s.toFixed(2) : "0.00"}
          <span className="rate-unit">% of budget</span>
        </span>
      </div>
      {airtime ? <AirtimeBar buckets={airtime.buckets} /> : <div className="classbar empty" />}
      <div className="kv-grid">
        <span>Coordinator</span>
        <span className="clip mono">{flow.coordinator}</span>
        <span>Airtime</span>
        <span>
          {airtime
            ? `${Math.round(airtime.us_per_s_60s)} µs/s · ${airtime.airtime_pct_60s.toFixed(3)}% duty (${airtime.provenance})`
            : "—"}
        </span>
        <span>Wire RTT</span>
        <span>
          {latency
            ? `p50 ${latency.p50_ms} ms · p95 ${latency.p95_ms} ms · max ${latency.max_ms} ms (${latency.count})`
            : "—"}
        </span>
        <span>Delivery</span>
        <span>
          {wire.delivery_ok} ok
          {wire.delivery_failed > 0 ? ` · ${wire.delivery_failed} failed` : " · 0 failed"}
          {wire.pending_sends > 0 ? ` · ${wire.pending_sends} in flight` : ""}
        </span>
        <span>Mesh health</span>
        <span>
          {wire.route_records} route records
          {Object.keys(wire.route_errors).length > 0
            ? ` · route errors ${Object.entries(wire.route_errors)
                .map(([code, count]) => `${code}×${count}`)
                .join(", ")}`
            : " · 0 route errors"}
        </span>
        <span>Last hop</span>
        <span>
          {wire.lqi_ewma != null
            ? `LQI ${Math.round(wire.lqi_ewma)} · RSSI ${Math.round(wire.rssi_ewma ?? 0)} dBm`
            : "—"}
        </span>
        <span>Link</span>
        <span>
          {flow.data_frames} frames · {flow.crc_errors} CRC · {flow.retransmits} reTx ·{" "}
          {wire.loopbacks} loopbacks
          {wire.layout_mismatch > 0 && (
            <span className="chip warn"> {wire.layout_mismatch} layout mismatches</span>
          )}
        </span>
      </div>
      <div className="kinds">
        {ezspTop.map(([name, count]) => (
          <span key={name} className="kind">
            <span className="kind-name">{name}</span>
            <span className="kind-count">{count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

export default function Wire() {
  const [tap, setTap] = useState<TapStats | null>(null);
  const [now, setNow] = useState(0);
  const [seconds, setSeconds] = useState(3600);
  const [stored, setStored] = useState<AirtimeWindow | null>(null);
  const [socketState, setSocketState] = useState<"connecting" | "open" | "closed">("connecting");

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
        setTap(parsed.tap);
        setNow(parsed.ts);
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

  const refreshStored = useCallback(async () => {
    try {
      setStored(await api<AirtimeWindow>(`/api/airtime?seconds=${seconds}`));
    } catch {
      /* keep the previous window on a transient error */
    }
  }, [seconds]);

  useEffect(() => {
    void refreshStored();
    const interval = window.setInterval(() => void refreshStored(), 30000);
    return () => window.clearInterval(interval);
  }, [refreshStored]);

  const flows = [...(tap?.flows ?? [])].sort((a, b) =>
    (a.instance ?? a.coordinator).localeCompare(b.instance ?? b.coordinator),
  );
  const totalCrc = flows.reduce((sum, flow) => sum + flow.crc_errors, 0);
  const agents = tap?.agents ?? 0;
  const storedInstances = Object.keys(stored?.instances ?? {}).sort();

  return (
    <>
      <div className={`banner ${agents > 0 ? "ok" : "warn"}`}>
        <span>
          Wire tap: <strong>{agents > 0 ? `${agents} agent${agents > 1 ? "s" : ""} streaming` : socketState === "open" ? "no agents connected" : socketState}</strong>
          {agents > 0
            ? ` · ${flows.length} coordinator flow${flows.length === 1 ? "" : "s"} · ${totalCrc} CRC errors`
            : ""}
        </span>
        <span className="hint">passive pcap → ASH/EZSP decode (T2)</span>
      </div>

      {flows.length === 0 ? (
        <div className="panel">
          <p className="panel-kicker">Waiting for wire frames</p>
          <p className="hint">
            A ninja-tap agent streams filtered pcap of each coordinator's TCP link to the
            collector, which decodes ASH/EZSP centrally. Flows appear as soon as an agent
            connects and traffic crosses a discovered coordinator endpoint.
          </p>
        </div>
      ) : (
        <div className="cards">
          {flows.map((flow) => (
            <FlowCard
              key={flow.coordinator}
              flow={flow}
              airtime={flow.instance ? tap?.airtime[flow.instance] : undefined}
              latency={flow.instance ? tap?.latency[flow.instance] : undefined}
              now={now}
            />
          ))}
        </div>
      )}

      <div className="panel">
        <div className="toolbar">
          <p className="panel-kicker">Airtime by bucket (stored rollups)</p>
          <div className="segmented">
            {WINDOWS.map(([label, value]) => (
              <button
                key={value}
                className={value === seconds ? "seg-btn active" : "seg-btn"}
                onClick={() => setSeconds(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        {storedInstances.length === 0 ? (
          <p className="hint">No stored airtime windows yet — rollups land every 10 s.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Instance</th>
                {BUCKET_ORDER.map((bucket) => (
                  <th key={bucket} className="num">
                    {BUCKET_LABELS[bucket]}
                  </th>
                ))}
                <th className="num">µs/s</th>
                <th className="num">of budget</th>
              </tr>
            </thead>
            <tbody>
              {storedInstances.map((instance) => {
                const view = stored!.instances[instance];
                return (
                  <tr key={instance}>
                    <td className="mono">{instance}</td>
                    {BUCKET_ORDER.map((bucket) => (
                      <td key={bucket} className="num">
                        {view.buckets[bucket]
                          ? `${(view.buckets[bucket].airtime_us / 1000).toFixed(0)} ms`
                          : "—"}
                      </td>
                    ))}
                    <td className="num">{Math.round(view.us_per_s)}</td>
                    <td className="num">{view.budget_pct.toFixed(2)}%</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <p className="hint">
          Airtime is reconstructed per frame from exact wire-tier payload sizes plus
          deterministic 802.15.4/Zigbee header arithmetic; groupcast cost includes mesh
          amplification across the instance's router census. The budget denominator is the
          CSMA-discounted channel (η = 0.7).
        </p>
      </div>
    </>
  );
}
