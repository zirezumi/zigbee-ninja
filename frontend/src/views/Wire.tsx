import { useCallback, useEffect, useState } from "react";
import {
  AirtimeLive,
  AirtimeWindow,
  api,
  FleetMessage,
  fleetSocketUrl,
  FusionView,
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
  tx_unicast: "sent (unicast)",
  tx_groupcast: "sent (group)",
  rx: "received",
  rx_mesh: "mesh overhead",
};
const BUCKET_TITLES: Record<string, string> = {
  tx_unicast: "Commands sent to a single device",
  tx_groupcast: "Commands sent to a group; relayed by every router, so far costlier on air",
  rx: "Frames arriving at the coordinator (reports, replies)",
  rx_mesh: "Routing bookkeeping: path reports and route-error notices",
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

function fusionText(fusion: FusionView | undefined): string {
  if (!fusion || fusion.state === "idle") return "—";
  if (fusion.state === "awaiting probe v0.4") {
    return "awaiting probe v0.4: update the extension from Permissions";
  }
  if (fusion.state === "no wire coverage") return "no wire frames to fuse with";
  const offset =
    fusion.clock_offset_ms !== null ? ` · clocks Δ${fusion.clock_offset_ms} ms` : "";
  return (
    `${fusion.matched_5m} matched · ${fusion.wire_only_5m} wire-only · ` +
    `${fusion.probe_only_5m} probe-only (5 min)${offset}`
  );
}

interface FlowCardProps {
  flow: WireFlow;
  airtime?: AirtimeLive;
  latency?: WireLatencyStats;
  fusion?: FusionView;
  now: number;
}

function FlowCard({ flow, airtime, latency, fusion, now }: FlowCardProps) {
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
        <span title="The network address of this coordinator's Zigbee radio">Coordinator</span>
        <span className="clip mono">{flow.coordinator}</span>
        <span title="Radio time this coordinator's traffic occupied over the last 60 s">
          Airtime
        </span>
        <span>
          {airtime
            ? `${Math.round(airtime.us_per_s_60s)} µs/s · ${airtime.airtime_pct_60s.toFixed(3)}% duty (${airtime.provenance})`
            : "—"}
        </span>
        <span title="Time from handing a command to the coordinator until the mesh confirms delivery">
          Wire round-trip
        </span>
        <span>
          {latency
            ? `p50 ${latency.p50_ms} ms · p95 ${latency.p95_ms} ms · max ${latency.max_ms} ms (${latency.count})`
            : "—"}
        </span>
        <span title="Commands the mesh confirmed delivered vs failed; 'in flight' = sent, confirmation not yet seen">
          Delivery
        </span>
        <span>
          {wire.delivery_ok} ok
          {wire.delivery_failed > 0 ? ` · ${wire.delivery_failed} failed` : " · 0 failed"}
          {wire.pending_sends > 0 ? ` · ${wire.pending_sends} in flight` : ""}
        </span>
        <span title="Route records are devices reporting their path back to the coordinator (normal bookkeeping); route errors are delivery paths that broke">
          Mesh health
        </span>
        <span>
          {wire.route_records} route records
          {Object.keys(wire.route_errors).length > 0
            ? ` · route errors ${Object.entries(wire.route_errors)
                .map(([code, count]) => `${code}×${count}`)
                .join(", ")}`
            : " · 0 route errors"}
        </span>
        <span title="Radio quality of the final hop into the coordinator: LQI is link quality 0–255 (higher is better); RSSI is signal strength in dBm (closer to 0 is stronger)">
          Last hop
        </span>
        <span>
          {wire.lqi_ewma != null
            ? `LQI ${Math.round(wire.lqi_ewma)} · RSSI ${Math.round(wire.rssi_ewma ?? 0)} dBm`
            : "—"}
        </span>
        <span title="Share of unicast transmissions this coordinator's radio had to repeat (measured from its own counters). Every retry re-burns the frame's airtime, so this scales the unicast cost.">
          Unicast retries
        </span>
        <span>
          {wire.retry_rate != null
            ? `${(wire.retry_rate * 100).toFixed(1)}% per hop (measured, ${wire.retry_rate_samples} window${wire.retry_rate_samples === 1 ? "" : "s"})`
            : "0% assumed; awaiting counter windows"}
        </span>
        <span title="How many times each router transmits a group/broadcast command on average, including automatic repeats: this multiplies the airtime cost of every group command across the mesh">
          Broadcast relays
        </span>
        <span title="Measured passively from the coordinator's own transmit counters. Counter windows polluted by relayed foreign traffic are discarded and counted, never guessed at.">
          {wire.avg_tx != null
            ? `×${wire.avg_tx} per router (measured, ${wire.avg_tx_samples} windows)`
            : wire.avg_tx_rejected > 0
              ? `×1.3 per router (modeled: ${wire.avg_tx_rejected} noisy window${wire.avg_tx_rejected === 1 ? "" : "s"} discarded)`
              : "×1.3 per router (modeled default; measuring)"}
        </span>
        <span title="Health of the Zigbee2MQTT ↔ coordinator link itself">Link</span>
        <span>
          {flow.data_frames} frames ·{" "}
          <span title="Frames that failed their integrity checksum (CRC): should be 0">
            {flow.crc_errors} corrupted
          </span>{" "}
          ·{" "}
          <span title="Frames the link had to resend (its own reliability layer)">
            {flow.retransmits} link retries
          </span>{" "}
          ·{" "}
          <span title="The coordinator hearing its own group commands echo back: excluded from radio airtime">
            {wire.loopbacks} loopbacks
          </span>
          {wire.layout_mismatch > 0 && (
            <span
              className="chip warn"
              title="Frames whose internal structure didn't match the expected firmware layout: counted instead of risking wrong numbers"
            >
              {" "}
              {wire.layout_mismatch} layout mismatches
            </span>
          )}
        </span>
        <span title="The same incoming frame seen at both the wire and inside Zigbee2MQTT, joined on the sender and its message sequence. Wire-only = frames Z2M handled without emitting a device event; probe-only = frames the capture missed. Matched pairs also measure the probe↔wire clock offset.">
          Frame fusion
        </span>
        <span>{fusionText(fusion)}</span>
        <span title="Internal tallies the coordinator chip keeps (transmissions, retries, failures). Zigbee2MQTT polls them hourly; zigbee-ninja reads the answers passively.">
          Coordinator counters
        </span>
        <span>
          {wire.counters
            ? Object.entries(wire.counters)
                .sort(([, a], [, b]) => b - a)
                .slice(0, 4)
                .map(([name, value]) => `${name} ${value}`)
                .join(" · ")
            : "awaiting a Z2M counter poll"}
        </span>
      </div>
      <div
        className="kinds"
        title="Most frequent operations crossing the coordinator link (EZSP frame types)"
      >
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
  const [fusion, setFusion] = useState<Record<string, FusionView>>({});
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
        setFusion(parsed.fusion ?? {});
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
          Wiretap:{" "}
          <strong title="A capture daemon is the small ninja-tap process installed on a host that can see the coordinators' network traffic; it streams a filtered packet capture to the collector, which does all the decoding">
            {agents > 0
              ? `${agents} capture daemon${agents > 1 ? "s" : ""} connected`
              : socketState === "open"
                ? "no capture daemons connected"
                : socketState}
          </strong>
          {agents > 0 && (
            <span>
              {` · watching ${flows.length} coordinator link${flows.length === 1 ? "" : "s"}`}
            </span>
          )}
          {agents > 0 && (
            <span title="Frames that failed their integrity checksum (CRC) on a coordinator link: should be 0; corruption means data lost between Zigbee2MQTT and the radio">
              {totalCrc > 0 ? ` · ${totalCrc} corrupted frames` : " · no corrupted frames"}
            </span>
          )}
        </span>
        <span className="hint">reads the coordinator links passively; it never transmits</span>
      </div>

      {flows.length === 0 ? (
        <div className="panel">
          <p className="panel-kicker">Waiting for wire frames</p>
          <p className="hint">
            A ninja-tap capture daemon streams a filtered packet capture of each coordinator's
            network link to the collector, which decodes it centrally. Flows appear as soon as
            a daemon connects and traffic crosses a discovered coordinator endpoint.
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
              fusion={flow.instance ? fusion[flow.instance] : undefined}
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
          <p className="hint">No stored airtime windows yet: rollups land every 10 s.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Instance</th>
                {BUCKET_ORDER.map((bucket) => (
                  <th key={bucket} className="num" title={BUCKET_TITLES[bucket]}>
                    {BUCKET_LABELS[bucket]}
                  </th>
                ))}
                <th className="num" title="Microseconds of radio time per second of wall clock">
                  µs/s
                </th>
                <th className="num" title="Share of the channel's usable capacity">
                  of budget
                </th>
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
          Airtime is reconstructed frame by frame: the exact payload sizes seen at the
          wiretap plus the fixed radio-header overhead every frame carries on air. Group
          commands are costed across every router that relays them. "Of budget" compares
          against the channel's usable capacity: 70% of the raw 250 kbps, the rest lost to
          listen-before-talk.
        </p>
      </div>
    </>
  );
}
