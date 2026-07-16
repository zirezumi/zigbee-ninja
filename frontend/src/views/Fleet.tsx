import { useEffect, useRef, useState, type ReactNode } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import {
  AirtimeLive,
  AlertBrief,
  BrokerView,
  FleetMessage,
  HeadroomInstance,
  HeadroomView,
  InstanceInfo,
  JournalEntry,
  JournalView,
  LatencyStats,
  ProbeStats,
  Tile,
  WireLatencyStats,
  api,
  fleetSocketUrl,
} from "../api";

const KIND_ORDER = ["command", "state", "bridge", "availability", "probe", "other"];
const HISTORY_LENGTH = 300; // seconds of 1 s samples the live chart accumulates

const KIND_TITLES: Record<string, string> = {
  command: "Commands sent to devices (…/set and …/get topics)",
  state: "State updates published by devices",
  bridge: "Zigbee2MQTT's own bridge topics (logs, registries, health)",
  availability: "Device online/offline transitions",
  probe: "zigbee-ninja's own telemetry (always accounted separately)",
  other: "Everything else under this instance's base topic",
};

function totalPerSecond(kinds: Record<string, number> | undefined): number {
  if (!kinds) return 0;
  return Object.entries(kinds)
    .filter(([kind]) => kind !== "total_60s")
    .reduce((sum, [, count]) => sum + count, 0);
}

/** Live message-rate histogram: 1 s buckets streamed from the fleet socket,
 * stepped/filled like a histogram, with labeled time and rate axes. */
function RateChart({ history, endTs }: { history: number[]; endTs: number }) {
  const host = useRef<HTMLDivElement | null>(null);
  const plot = useRef<uPlot | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const options: uPlot.Options = {
      width: Math.max(host.current.clientWidth, 360),
      height: 150,
      scales: {
        x: { time: true },
        y: { range: (_u, _min, max) => [0, Math.max(max, 5)] },
      },
      axes: [
        {
          stroke: "#8b95a7",
          grid: { stroke: "rgba(128, 136, 152, 0.15)" },
          ticks: { stroke: "rgba(128, 136, 152, 0.25)" },
        },
        {
          label: "messages / s",
          stroke: "#8b95a7",
          grid: { stroke: "rgba(128, 136, 152, 0.15)" },
          ticks: { stroke: "rgba(128, 136, 152, 0.25)" },
        },
      ],
      series: [
        {},
        {
          label: "msg/s",
          stroke: "#4cc38a",
          fill: "rgba(76, 195, 138, 0.18)",
          paths: uPlot.paths?.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined,
        },
      ],
      legend: { show: false },
      cursor: { show: false },
    };
    plot.current = new uPlot(options, [[], []], host.current);
    return () => {
      plot.current?.destroy();
      plot.current = null;
    };
  }, []);

  useEffect(() => {
    if (!plot.current) return;
    const xs = history.map((_, index) => endTs - (history.length - 1 - index));
    plot.current.setData([xs, history]);
  }, [history, endTs]);

  return <div ref={host} className="rate-chart" />;
}

interface Coverage {
  t0: boolean;
  t1: boolean;
  t2: boolean;
}

function CoverageMeter({ coverage }: { coverage: Coverage }) {
  const tiers: Array<[label: string, live: boolean, title: string]> = [
    [
      "MQTT firehose",
      coverage.t0,
      "The broker connection itself: sees every MQTT command and state publish",
    ],
    [
      "Z2M extension probe",
      coverage.t1,
      "A small probe running inside Zigbee2MQTT, reporting every Zigbee frame it handles: including housekeeping that never reaches MQTT",
    ],
    [
      "Wiretap",
      coverage.t2,
      "Passive capture of the coordinator's network link: exact frame bytes and timing, decoded by the collector",
    ],
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

function alertChip(alerts: AlertBrief[]): { label: string; className: string } | null {
  if (alerts.length === 0) return null;
  const worst = alerts.some((alert) => alert.severity === "critical")
    ? "critical"
    : alerts.some((alert) => alert.severity === "warning")
      ? "warning"
      : "info";
  return {
    label: alerts.length === 1 ? (alerts[0].name ?? "1 alert") : `${alerts.length} alerts`,
    className: worst === "critical" ? "chip bad" : worst === "warning" ? "chip warn" : "chip",
  };
}

function Fact({
  label,
  title,
  children,
}: {
  label: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="fact" title={title}>
      <span className="fact-label">{label}</span>
      <span className="fact-value">{children}</span>
    </div>
  );
}

function ProbeFactValue({
  tile,
  busy,
  onUpdate,
}: {
  tile?: Tile;
  busy: boolean;
  onUpdate: () => void;
}) {
  if (!tile || !["deployed", "deploying"].includes(tile.status)) {
    return <span>Not deployed: deploy from Permissions</span>;
  }
  if (tile.status === "deploying") return <span>Deploying…</span>;
  if (tile.drift) {
    return (
      <span>
        Deployed ·{" "}
        <button
          className="linkish"
          disabled={busy}
          title={`Replace the running v${tile.probe.version} extension with the bundled v${tile.bundled_version} in place: the grant is untouched`}
          onClick={onUpdate}
        >
          {busy ? "updating…" : `Update to v${tile.bundled_version}`}
        </button>
      </span>
    );
  }
  return <span title={`v${tile.probe.version ?? tile.version}`}>Deployed · up to date</span>;
}

interface InstanceRowProps {
  instance: InstanceInfo;
  kinds: Record<string, number> | undefined;
  history: number[];
  endTs: number;
  latency?: LatencyStats;
  probe?: ProbeStats;
  tile?: Tile;
  probeBusy: boolean;
  onProbeUpdate: () => void;
  airtime?: AirtimeLive;
  wireLatency?: WireLatencyStats;
  headroom?: HeadroomInstance;
  coverage: Coverage;
  alerts: AlertBrief[];
}

function InstanceRow({
  instance,
  kinds,
  history,
  endTs,
  latency,
  probe,
  tile,
  probeBusy,
  onProbeUpdate,
  airtime,
  wireLatency,
  headroom,
  coverage,
  alerts,
}: InstanceRowProps) {
  const online = instance.online;
  const chip = alertChip(alerts);
  return (
    <div className="instance-row">
      <div className="instance-head">
        <span className={online === false ? "dot off" : online ? "dot on" : "dot unknown"} />
        <span className="instance-name">{instance.base_topic}</span>
        {chip && (
          <span className={chip.className} title={alerts.map((a) => a.name).join(", ")}>
            {chip.label}
          </span>
        )}
        <span className="rate-big">
          {totalPerSecond(kinds)}
          <span className="rate-unit">msg/s</span>
        </span>
      </div>
      <RateChart history={history} endTs={endTs} />
      <div className="facts">
        <Fact
          label="Zigbee2MQTT"
          title="Version this instance reported on its bridge/info topic"
        >
          {instance.version ?? "—"}
        </Fact>
        <Fact
          label="Channel"
          title="Zigbee radio channel (11–26, in the 2.4 GHz band). Instances on the same channel share one pool of airtime."
        >
          {instance.channel ?? "—"}
        </Fact>
        <Fact
          label="Adapter"
          title="The coordinator radio hardware, and the network address or serial port Zigbee2MQTT reaches it at"
        >
          <span className="clip" title={instance.adapter_port ?? undefined}>
            {instance.coordinator_type ?? "—"}
            {instance.adapter_port ? ` · ${instance.adapter_port}` : ""}
          </span>
        </Fact>
        <Fact
          label="Devices"
          title="Devices paired to this coordinator. Routers (mains-powered) relay traffic for the mesh; end devices (usually battery) don't."
        >
          {instance.device_count} ({instance.router_count} routers, {instance.end_device_count}{" "}
          end devices)
        </Fact>
        <Fact
          label="Groups"
          title="Zigbee groups on this instance. A single group command is relayed by every router, so heavy group use multiplies airtime."
        >
          {instance.group_count}
        </Fact>
        <Fact
          label="Probe"
          title="zigbee-ninja's extension running inside this Zigbee2MQTT instance: deployed and removed from the Permissions page"
        >
          {probe?.enabled === false ? (
            <span>Deployed · paused</span>
          ) : (
            <ProbeFactValue tile={tile} busy={probeBusy} onUpdate={onProbeUpdate} />
          )}
        </Fact>
        <Fact
          label="Airtime"
          title="Share of the radio channel's usable capacity this coordinator's traffic occupied over the last 60 s, measured at the wiretap"
        >
          {airtime
            ? `${airtime.budget_pct_60s.toFixed(2)}% of budget · ${Math.round(airtime.us_per_s_60s)} µs/s`
            : "—"}
        </Fact>
        <Fact
          label="Wire round-trip"
          title="Time from handing a command to the coordinator until the mesh confirms delivery, measured on the coordinator link (the most accurate latency view)"
        >
          {wireLatency
            ? `p50 ${wireLatency.p50_ms} ms · p95 ${wireLatency.p95_ms} ms (${wireLatency.count})`
            : "—"}
        </Fact>
        <Fact
          label="Capacity limit"
          title="Maximum sustainable command rate, measured by the Calibration benchmark. ≥ means the benchmark ended before anything degraded, so the true limit is at least this. 'used' compares the trailing hour's load against it."
        >
          {headroom?.knee
            ? `${headroom.knee.kind === "mesh_knee" ? "" : "≥"}${headroom.knee.eps}/s` +
              (headroom.headroom ? ` · ${headroom.headroom.knee_utilization_pct}% used` : "")
            : "not calibrated"}
        </Fact>
        <Fact
          label="Z2M echo"
          title="Time from an MQTT command to the device's state echo, seen at the Zigbee2MQTT boundary (approximate: includes queueing in Z2M)"
        >
          {latency
            ? `p50 ${latency.p50_ms} ms · p95 ${latency.p95_ms} ms (${latency.count})`
            : "—"}
        </Fact>
      </div>
      <div className="row-foot">
        <CoverageMeter coverage={coverage} />
        <div className="kinds">
          {KIND_ORDER.map((kind) => (
            <span key={kind} className="kind" title={KIND_TITLES[kind]}>
              <span className="kind-name">{kind}</span>
              <span className="kind-count">{kinds?.[kind] ?? 0}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

const JOURNAL_KIND_LABELS: Record<string, string> = {
  device_added: "Device added",
  device_removed: "Device removed",
  device_renamed: "Device renamed",
  device_rejoined: "Device rejoined",
  group_added: "Group added",
  group_removed: "Group removed",
  group_renamed: "Group renamed",
  group_membership_changed: "Group membership changed",
  z2m_version_changed: "Zigbee2MQTT updated",
  channel_changed: "Radio channel changed",
  coordinator_firmware_changed: "Coordinator firmware changed",
};

function ago(ts: number): string {
  const elapsed = Math.max(0, Date.now() / 1000 - ts);
  if (elapsed < 60) return "just now";
  if (elapsed < 3600) return `${Math.round(elapsed / 60)} min ago`;
  if (elapsed < 86400) return `${Math.round(elapsed / 3600)} h ago`;
  return `${Math.round(elapsed / 86400)} d ago`;
}

function journalDetail(entry: JournalEntry): string {
  const detail = entry.detail;
  const parts: string[] = [];
  if (typeof detail.from !== "undefined" && typeof detail.to !== "undefined") {
    parts.push(`${String(detail.from)} → ${String(detail.to)}`);
  }
  if (entry.kind === "device_added") {
    const label = [detail.vendor, detail.model].filter(Boolean).join(" ");
    if (label) parts.push(label);
    if (detail.moved_from) parts.push(`moved from ${String(detail.moved_from)}`);
  }
  if (entry.kind === "group_membership_changed") {
    const added = (detail.added as string[] | undefined) ?? [];
    const removed = (detail.removed as string[] | undefined) ?? [];
    if (added.length) parts.push(`+${added.join(", +")}`);
    if (removed.length) parts.push(`-${removed.join(", -")}`);
    parts.push(`now ${String(detail.size)} members`);
  }
  return parts.join(" · ");
}

/** Change journal: configuration changes are the points where traffic
 * patterns can shift, so they anchor every before/after comparison. */
function RecentChanges() {
  const [entries, setEntries] = useState<JournalEntry[]>([]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api<JournalView>("/api/journal?seconds=604800&limit=15");
        if (alive) setEntries(data.entries);
      } catch {
        // panel simply stays empty until the next poll succeeds
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 30000);
    return () => {
      alive = false;
      window.clearInterval(interval);
    };
  }, []);

  return (
    <div className="panel">
      <p
        className="panel-kicker"
        title="Watched from the Zigbee2MQTT registries: no polling, no mesh traffic. Each entry marks a point where traffic patterns may have shifted."
      >
        Recent changes (7 days)
      </p>
      {entries.length === 0 ? (
        <p className="hint">
          No configuration changes seen. Devices joining or leaving, renames, group
          edits, channel moves, and Zigbee2MQTT or firmware upgrades appear here.
        </p>
      ) : (
        <table className="table">
          <tbody>
            {entries.map((entry, index) => (
              <tr key={`${entry.ts}/${index}`}>
                <td>{JOURNAL_KIND_LABELS[entry.kind] ?? entry.kind}</td>
                <td className="mono">{entry.subject}</td>
                <td className="mono">{entry.instance}</td>
                <td>{journalDetail(entry)}</td>
                <td className="num" title={new Date(entry.ts * 1000).toLocaleString()}>
                  {ago(entry.ts)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

interface FleetProps {
  onReconfigure: () => void;
  brokerInfo: BrokerView | null;
}

export default function Fleet({ onReconfigure, brokerInfo }: FleetProps) {
  const [message, setMessage] = useState<FleetMessage | null>(null);
  const [socketState, setSocketState] = useState<"connecting" | "open" | "closed">("connecting");
  const historyRef = useRef<Record<string, number[]>>({});

  const [headroom, setHeadroom] = useState<HeadroomView | null>(null);
  const [probeTiles, setProbeTiles] = useState<Record<string, Tile>>({});
  const [probeBusy, setProbeBusy] = useState<string | null>(null);

  const loadTiles = async () => {
    try {
      const data = await api<{ tiles: Tile[] }>("/api/tiles");
      setProbeTiles(
        Object.fromEntries(
          data.tiles
            .filter((tile) => tile.capability === "z2m_extension")
            .map((tile) => [tile.target, tile]),
        ),
      );
    } catch {
      // probe facts fall back to "not deployed" until the next poll
    }
  };

  useEffect(() => {
    void loadTiles();
    const interval = window.setInterval(() => void loadTiles(), 30000);
    return () => window.clearInterval(interval);
  }, []);

  async function updateProbe(base: string) {
    setProbeBusy(base);
    try {
      await api("/api/tiles/deploy", {
        method: "POST",
        body: JSON.stringify({ capability: "z2m_extension", target: base }),
      });
    } catch {
      // the tile poll below shows the true state either way
    } finally {
      setProbeBusy(null);
      void loadTiles();
    }
  }

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api<HeadroomView>("/api/headroom?seconds=3600");
        if (alive) setHeadroom(data);
      } catch {
        // capacity line simply stays absent until the next poll succeeds
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
  const alerts = message?.alerts ?? [];
  const globalAlerts = alerts.filter((alert) => alert.instance === "*");
  const brokerAddress = brokerInfo?.host
    ? `${brokerInfo.host}:${brokerInfo.port ?? 1883}`
    : null;

  return (
    <>
      <div className={`banner ${broker?.state === "connected" ? "ok" : "warn"}`}>
        <span>
          Broker: <strong>{broker?.state ?? socketState}</strong>
          {brokerAddress ? ` (${brokerAddress})` : ""}
          {broker?.error ? `: ${broker.error}` : ""}
          {broker?.state === "connected" ? (
            <span title="Total message rate across every topic on this broker: Zigbee2MQTT traffic and everything else sharing it">
              {` · ${globalRate} msg/s`}
            </span>
          ) : (
            ""
          )}
        </span>
        <button className="ghost small" onClick={onReconfigure}>
          Reconfigure
        </button>
      </div>
      {globalAlerts.length > 0 && (
        <div className="banner warn">
          <span>
            {globalAlerts.map((alert) => (
              <span
                key={`${alert.instance}/${alert.name}`}
                className={alert.severity === "critical" ? "chip bad" : "chip warn"}
              >
                {alert.name}
              </span>
            ))}
          </span>
        </div>
      )}

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
        <div className="instance-rows">
          {instances.map((instance) => {
            const base = instance.base_topic;
            const probe = message?.probes[base];
            const heartbeat = probe?.last_heartbeat_at;
            const flow = message?.tap.flows.find((candidate) => candidate.instance === base);
            const now = message?.ts ?? 0;
            return (
              <InstanceRow
                key={base}
                instance={instance}
                kinds={message?.rates[base]}
                history={historyRef.current[base] ?? []}
                endTs={now}
                latency={message?.latency[base]}
                probe={probe}
                tile={probeTiles[base]}
                probeBusy={probeBusy === base}
                onProbeUpdate={() => void updateProbe(base)}
                airtime={message?.tap.airtime[base]}
                wireLatency={message?.tap.latency[base]}
                headroom={headroom?.instances[base]}
                coverage={{
                  t0: broker?.state === "connected",
                  t1: heartbeat != null && now - heartbeat < 120,
                  t2: flow != null && now - flow.last_seen < 60,
                }}
                alerts={alerts.filter((alert) => alert.instance === base)}
              />
            );
          })}
        </div>
      )}

      <RecentChanges />
    </>
  );
}
