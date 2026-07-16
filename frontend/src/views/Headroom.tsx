import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { api, HeadroomInstance, HeadroomView } from "../api";

const WINDOWS: Array<[string, number]> = [
  ["1 h", 3600],
  ["6 h", 21600],
  ["24 h", 86400],
];

function kneeLabel(instance: HeadroomInstance): string {
  if (!instance.knee) return "not calibrated";
  const prefix = instance.knee.kind === "mesh_knee" ? "" : "≥";
  return `${prefix}${instance.knee.eps}/s`;
}

/** Wire p95 latency vs TX load, one point per rollup window, with the
 * calibrated knee as a dashed vertical line: points bending upward well
 * below the line are the continuous knee-validation signal (§10). */
function KneeScatter({
  points,
  kneeEps,
}: {
  points: Array<{ eps: number; p95_ms: number }>;
  kneeEps: number | null;
}) {
  const host = useRef<HTMLDivElement | null>(null);
  const plot = useRef<uPlot | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const sorted = [...points].sort((a, b) => a.eps - b.eps);
    const data: uPlot.AlignedData = [
      sorted.map((point) => point.eps),
      sorted.map((point) => point.p95_ms),
    ];
    const maxEps = sorted.length ? sorted[sorted.length - 1].eps : 1;
    const xMax = Math.max(maxEps, kneeEps !== null ? kneeEps * 1.08 : 0, 1);
    const options: uPlot.Options = {
      width: Math.max(host.current.clientWidth, 360),
      height: 220,
      // Log latency axis: rare multi-second delivery tails stay visible
      // without crushing the sub-100 ms bulk of the distribution.
      scales: { x: { time: false, range: [0, xMax] }, y: { distr: 3, log: 10 } },
      axes: [
        {
          label: "TX load (frames/s, per rollup window)",
          stroke: "#8b95a7",
          grid: { stroke: "rgba(128, 136, 152, 0.15)" },
          ticks: { stroke: "rgba(128, 136, 152, 0.25)" },
        },
        {
          label: "wire p95 latency (ms)",
          stroke: "#8b95a7",
          grid: { stroke: "rgba(128, 136, 152, 0.15)" },
          ticks: { stroke: "rgba(128, 136, 152, 0.25)" },
        },
      ],
      series: [
        {},
        {
          paths: () => null,
          points: { show: true, size: 5, fill: "rgba(76, 195, 138, 0.65)", stroke: "#4cc38a" },
        },
      ],
      legend: { show: false },
      cursor: { show: false },
      hooks:
        kneeEps === null
          ? {}
          : {
              draw: [
                (u) => {
                  const x = u.valToPos(kneeEps, "x", true);
                  const ctx = u.ctx;
                  ctx.save();
                  ctx.strokeStyle = "rgba(229, 72, 77, 0.85)";
                  ctx.setLineDash([5, 5]);
                  ctx.lineWidth = 1.5;
                  ctx.beginPath();
                  ctx.moveTo(x, u.bbox.top);
                  ctx.lineTo(x, u.bbox.top + u.bbox.height);
                  ctx.stroke();
                  ctx.restore();
                },
              ],
            },
    };
    plot.current = new uPlot(options, data, host.current);
    return () => {
      plot.current?.destroy();
      plot.current = null;
    };
  }, [points, kneeEps]);

  return <div ref={host} />;
}

function InstancePanel({ base, view }: { base: string; view: HeadroomInstance }) {
  const knee = view.knee;
  const dens = view.denominators;
  return (
    <div className="panel">
      <div className="toolbar">
        <p className="panel-kicker">{base}</p>
        {knee ? (
          <>
            <span
              className="chip ok"
              title="The calibrated maximum sustainable command rate; ≥ marks a lower bound: the benchmark ended before anything degraded"
            >
              capacity limit {kneeLabel(view)}
              {knee.mode === "spread" ? " · whole coordinator" : " · single device"}
            </span>
            {knee.kind === "pipeline_ceiling" && (
              <span
                className="chip"
                title="The benchmark driver could no longer add load: the Zigbee2MQTT/driver software pipeline is the binding constraint, so the radio's true limit is at least this"
              >
                software pipeline bound
              </span>
            )}
            {knee.stale_environment && (
              <span className="chip warn">firmware changed since calibration: recalibrate?</span>
            )}
          </>
        ) : (
          <span className="chip warn">not calibrated</span>
        )}
      </div>
      <div className="kv-grid">
        <span title="Share of the radio channel's usable capacity this instance consumed over the window: capacity view 1 of 3">
          Channel budget
        </span>
        <span>
          {dens.channel_budget.pct}% · {Math.round(dens.channel_budget.us_per_s)} µs/s
        </span>
        <span title="Maximum command rate the coordinator sustained in a whole-coordinator (spread) benchmark; ≥ means only bounded from below: capacity view 2 of 3">
          Coordinator limit
        </span>
        <span>
          {dens.ncp_knee
            ? `${dens.ncp_knee.provenance === "lower_bound" ? "≥" : ""}${dens.ncp_knee.eps}/s (${dens.ncp_knee.provenance})`
            : "—"}
        </span>
        <span title="Zigbee2MQTT serves each device from its own queue; this is the rate where a single-device benchmark saturated: capacity view 3 of 3">
          Per-device ceiling
        </span>
        <span>
          {dens.pipeline ? `${dens.pipeline.eps}/s per device` : "not reached"}
        </span>
        <span title="Observed command load over the selected window">Load</span>
        <span>
          {view.rates
            ? `p50 ${view.rates.p50_eps}/s · p95 ${view.rates.p95_eps}/s · max ${view.rates.max_eps}/s`
            : "no traffic in window"}
        </span>
        <span title="How much capacity remains: steady = limit minus typical (p95) load; burst = limit minus the worst peak">
          Headroom
        </span>
        <span title={view.headroom ? view.headroom.granularity : undefined}>
          {view.headroom
            ? `steady ${view.headroom.steady_eps}/s · burst ${view.headroom.burst_eps}/s · ` +
              `${view.headroom.knee_utilization_pct}% of capacity used`
            : "—"}
        </span>
      </div>
      {view.scatter.length >= 2 ? (
        <KneeScatter points={view.scatter} kneeEps={knee ? knee.eps : null} />
      ) : (
        <p className="hint">Not enough latency-vs-load windows yet for the scatter.</p>
      )}
      {knee && (
        <p className="hint">
          Calibrated against {knee.target ?? "?"} ({knee.rtt_source ?? "?"} RTT
          {knee.breach ? `, ended by ${knee.breach}` : ""}). Each dot is one rollup window:
          command load across, delivery latency up. Dots bending upward well left of the
          dashed capacity line would mean the mesh now degrades below its calibrated
          limit: the cue to recalibrate.
        </p>
      )}
    </div>
  );
}

export default function Headroom() {
  const [seconds, setSeconds] = useState(21600);
  const [view, setView] = useState<HeadroomView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api<HeadroomView>(`/api/headroom?seconds=${seconds}`);
        if (alive) {
          setView(data);
          setError(null);
        }
      } catch {
        if (alive) setError("Failed to load headroom data");
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 30000);
    return () => {
      alive = false;
      window.clearInterval(interval);
    };
  }, [seconds]);

  const bases = view ? Object.keys(view.instances).sort() : [];

  return (
    <>
      <div className="toolbar">
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
        <span className="hint">
          Three views of capacity side by side: radio airtime, the coordinator's measured
          limit, and the per-device software ceiling: with headroom against the calibrated
          limit and a latency-vs-load scatter that keeps validating it.
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {view === null ? (
        <p className="hint">loading…</p>
      ) : bases.length === 0 ? (
        <div className="panel">
          <p className="hint">No load or calibration data yet.</p>
        </div>
      ) : (
        bases.map((base) => (
          <InstancePanel key={base} base={base} view={view.instances[base]} />
        ))
      )}
    </>
  );
}
