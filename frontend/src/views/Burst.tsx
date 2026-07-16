import { useCallback, useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import {
  BurstChain,
  BurstEvent,
  BurstTimeline,
  InstanceInfo,
  api,
} from "../api";

const WINDOW_PRESETS: Array<{ label: string; seconds: number }> = [
  { label: "5 m", seconds: 300 },
  { label: "15 m", seconds: 900 },
  { label: "1 h", seconds: 3600 },
  { label: "6 h", seconds: 21600 },
];
const EVENT_TABLE_WINDOW_S = 120; // zoom in this far to see raw rows
const TIMELINE_BINS = 300;

function bucketFor(seconds: number): number {
  return Math.max(10, Math.min(60000, Math.round((seconds * 1000) / TIMELINE_BINS)));
}

function formatClock(ts: number, withMs: boolean): string {
  const date = new Date(ts * 1000);
  const base = date.toLocaleTimeString([], { hour12: false });
  if (!withMs) return base;
  return `${base}.${String(Math.round((ts % 1) * 1000)).padStart(3, "0")}`;
}

function TimelineChart({
  timeline,
  onZoom,
}: {
  timeline: BurstTimeline;
  onZoom: (start: number, end: number) => void;
}) {
  const host = useRef<HTMLDivElement | null>(null);
  const plot = useRef<uPlot | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const bucketS = timeline.bucket_ms / 1000;
    const count = Math.max(1, Math.round((timeline.end - timeline.start) / bucketS));
    const xs: number[] = [];
    const mqtt: number[] = [];
    const wire: number[] = [];
    const byBin = new Map(timeline.bins.map((entry) => [entry.bin, entry]));
    for (let index = 0; index < count; index += 1) {
      xs.push(timeline.start + index * bucketS);
      mqtt.push(byBin.get(index)?.mqtt?.events ?? 0);
      wire.push(byBin.get(index)?.wire?.events ?? 0);
    }
    const options: uPlot.Options = {
      width: Math.max(host.current.clientWidth, 360),
      height: 220,
      scales: { x: { time: true } },
      axes: [
        {
          stroke: "#8b95a7",
          grid: { stroke: "rgba(128, 136, 152, 0.15)" },
          ticks: { stroke: "rgba(128, 136, 152, 0.25)" },
        },
        {
          label: `events per ${timeline.bucket_ms} ms bucket`,
          stroke: "#8b95a7",
          grid: { stroke: "rgba(128, 136, 152, 0.15)" },
          ticks: { stroke: "rgba(128, 136, 152, 0.25)" },
        },
      ],
      series: [
        {},
        {
          label: "MQTT",
          stroke: "#4cc38a",
          fill: "rgba(76, 195, 138, 0.18)",
          paths: uPlot.paths?.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined,
        },
        {
          label: "wire",
          stroke: "#f5a623",
          fill: "rgba(245, 166, 35, 0.14)",
          paths: uPlot.paths?.stepped ? uPlot.paths.stepped({ align: 1 }) : undefined,
        },
      ],
      cursor: { drag: { x: true, y: false } },
      hooks: {
        setSelect: [
          (u) => {
            if (u.select.width < 5) return;
            const start = u.posToVal(u.select.left, "x");
            const end = u.posToVal(u.select.left + u.select.width, "x");
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
            if (end - start >= 0.05) onZoom(start, end);
          },
        ],
      },
    };
    plot.current = new uPlot(options, [xs, mqtt, wire], host.current);
    return () => {
      plot.current?.destroy();
      plot.current = null;
    };
  }, [timeline, onZoom]);

  return <div ref={host} />;
}

export default function Burst() {
  const [instances, setInstances] = useState<InstanceInfo[]>([]);
  const [instance, setInstance] = useState<string>("");
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [presetSeconds, setPresetSeconds] = useState(900);
  const [timeline, setTimeline] = useState<BurstTimeline | null>(null);
  const [events, setEvents] = useState<BurstEvent[] | null>(null);
  const [chains, setChains] = useState<BurstChain[]>([]);
  const [, setZoomStack] = useState<Array<{ start: number; end: number }>>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const data = await api<{ instances: InstanceInfo[] }>("/api/instances");
        setInstances(data.instances);
        if (data.instances.length > 0) setInstance(data.instances[0].base_topic);
      } catch {
        setError("Failed to list instances");
      }
    })();
  }, []);

  const load = useCallback(async () => {
    if (!instance) return;
    const end = range?.end ?? Date.now() / 1000;
    const start = range?.start ?? end - presetSeconds;
    const seconds = end - start;
    try {
      const view = await api<BurstTimeline>(
        `/api/burst/timeline?instance=${encodeURIComponent(instance)}` +
          `&seconds=${Math.ceil(seconds)}&end=${end}&bucket_ms=${bucketFor(seconds)}`,
      );
      setTimeline(view);
      const chainView = await api<{ chains: BurstChain[] }>(
        `/api/burst/chains?instance=${encodeURIComponent(instance)}&start=${start}&end=${end}`,
      );
      setChains(chainView.chains);
      if (seconds <= EVENT_TABLE_WINDOW_S) {
        const eventView = await api<{ events: BurstEvent[] }>(
          `/api/burst/events?instance=${encodeURIComponent(instance)}` +
            `&start=${start}&end=${end}&limit=2000`,
        );
        setEvents(eventView.events);
      } else {
        setEvents(null);
      }
      setError(null);
    } catch {
      setError("Failed to load the burst window");
    }
  }, [instance, range, presetSeconds]);

  useEffect(() => {
    void load();
    if (range !== null) return undefined; // frozen window while zoomed
    const interval = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(interval);
  }, [load, range]);

  function zoomTo(start: number, end: number) {
    setZoomStack((stack) => (range ? [...stack, range] : stack));
    setRange({ start, end });
  }

  function zoomOut() {
    setZoomStack((stack) => {
      const previous = stack[stack.length - 1];
      setRange(previous ?? null);
      return stack.slice(0, -1);
    });
  }

  const windowSeconds = range
    ? range.end - range.start
    : presetSeconds;

  return (
    <>
      <div className="panel">
        <p className="panel-kicker">Event timeline</p>
        <p className="hint">
          Every observed event — MQTT messages and decoded coordinator-link frames — from
          the raw event store: the newest hour held in memory, older hours archived on disk
          (48 h by default, adjustable in Settings). Drag on the chart to zoom; windows of{" "}
          {EVENT_TABLE_WINDOW_S} s or less list the individual events.
        </p>
        <div className="row">
          <label>
            Instance
            <select value={instance} onChange={(event) => setInstance(event.target.value)}>
              {instances.map((info) => (
                <option key={info.base_topic} value={info.base_topic}>
                  {info.base_topic}
                </option>
              ))}
            </select>
          </label>
          <label>
            Window
            <span className="row">
              {WINDOW_PRESETS.map((preset) => (
                <button
                  key={preset.seconds}
                  type="button"
                  className={
                    !range && presetSeconds === preset.seconds ? "small" : "ghost small"
                  }
                  onClick={() => {
                    setRange(null);
                    setZoomStack([]);
                    setPresetSeconds(preset.seconds);
                  }}
                >
                  {preset.label}
                </button>
              ))}
              {range && (
                <button type="button" className="ghost small" onClick={zoomOut}>
                  ← zoom out
                </button>
              )}
            </span>
          </label>
        </div>
        {error && <p className="error">{error}</p>}
        {timeline && <TimelineChart timeline={timeline} onZoom={zoomTo} />}
        {timeline && (
          <p className="hint">
            {formatClock(timeline.start, windowSeconds < 60)} –{" "}
            {formatClock(timeline.end, windowSeconds < 60)} · bucket {timeline.bucket_ms} ms
            · store: {timeline.store.hot_rows} events in the newest hour ·{" "}
            {timeline.store.segments} archived hour{timeline.store.segments === 1 ? "" : "s"} (
            {(timeline.store.segment_bytes / 1_000_000).toFixed(1)} MB)
            {timeline.store.dropped > 0 ? ` · ${timeline.store.dropped} dropped` : ""}
          </p>
        )}
      </div>

      <div className="panel">
        <p className="panel-kicker">Command chains in window</p>
        {chains.length === 0 ? (
          <p className="hint">No command chains opened in this window.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Opened</th>
                <th>Target</th>
                <th title="set changes state; get reads it">Command</th>
                <th title="Who issued the command — the Home Assistant automation, script, or user when the HA integration is connected, else the MQTT client">
                  Commander
                </th>
                <th className="num" title="State publishes the command provoked">
                  Echoes
                </th>
                <th className="num" title="Command to first state echo — end-to-end responsiveness">
                  First echo
                </th>
              </tr>
            </thead>
            <tbody>
              {chains.map((chain, index) => (
                <tr key={index}>
                  <td className="mono">{formatClock(chain.opened_at, true)}</td>
                  <td className="mono">{chain.target}</td>
                  <td>{chain.verb}</td>
                  <td>{chain.client ?? "(unattributed)"}</td>
                  <td className="num">{chain.echo_count}</td>
                  <td className="num">
                    {chain.first_echo_ms !== null
                      ? `${chain.first_echo_ms.toFixed(0)} ms`
                      : "—"}
                    {chain.redundant ? <span className="chip warn"> redundant</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {events !== null && (
        <div className="panel">
          <p className="panel-kicker">Events ({events.length})</p>
          <table className="table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Source</th>
                <th>Kind</th>
                <th>Dir</th>
                <th>Target</th>
                <th className="num">Bytes</th>
              </tr>
            </thead>
            <tbody>
              {events.slice(0, 500).map((event, index) => (
                <tr key={index}>
                  <td className="mono">{formatClock(event.ts, true)}</td>
                  <td>{event.source}</td>
                  <td className="mono">{event.kind}</td>
                  <td>{event.direction}</td>
                  <td className="mono clip">{event.target ?? "—"}</td>
                  <td className="num">{event.size}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {events.length > 500 && (
            <p className="hint">Showing the first 500 of {events.length} — zoom further.</p>
          )}
        </div>
      )}
    </>
  );
}
