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
 * calibrated knee as a dashed vertical line — points bending upward well
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
            <span className="chip ok">
              knee {kneeLabel(view)}
              {knee.mode === "spread" ? " · NCP (spread)" : ""}
              {knee.kind === "pipeline_ceiling"
                ? knee.mode === "spread"
                  ? " · global pipeline ceiling"
                  : " · per-device pipeline ceiling"
                : ""}
            </span>
            {knee.stale_environment && (
              <span className="chip warn">firmware changed since calibration — recalibrate?</span>
            )}
          </>
        ) : (
          <span className="chip warn">not calibrated</span>
        )}
      </div>
      <div className="kv-grid">
        <span>Channel budget</span>
        <span title="Share of the CSMA-discounted 250 kbps channel over the window (denominator 1)">
          {dens.channel_budget.pct}% · {Math.round(dens.channel_budget.us_per_s)} µs/s
        </span>
        <span>NCP knee</span>
        <span title="Denominator 2 — a saturated or censored ramp only bounds it from below">
          {dens.ncp_knee
            ? `${dens.ncp_knee.provenance === "lower_bound" ? "≥" : ""}${dens.ncp_knee.eps}/s (${dens.ncp_knee.provenance})`
            : "—"}
        </span>
        <span>Pipeline ceiling</span>
        <span title="Denominator 3 — Zigbee2MQTT's per-device service rate where the ramp saturated">
          {dens.pipeline ? `${dens.pipeline.eps}/s per device` : "not reached"}
        </span>
        <span>Load</span>
        <span>
          {view.rates
            ? `p50 ${view.rates.p50_eps}/s · p95 ${view.rates.p95_eps}/s · max ${view.rates.max_eps}/s`
            : "no traffic in window"}
        </span>
        <span>Headroom</span>
        <span title={view.headroom ? view.headroom.granularity : undefined}>
          {view.headroom
            ? `steady ${view.headroom.steady_eps}/s · burst ${view.headroom.burst_eps}/s · ` +
              `${view.headroom.knee_utilization_pct}% of knee`
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
          {knee.breach ? `, ended by ${knee.breach}` : ""}). Points bending upward well left
          of the dashed knee line would mean the mesh degrades below its calibrated
          capacity — the continuous validation signal.
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
          Three capacity denominators side by side, with headroom against the calibrated
          knee and the latency-vs-load scatter that validates it continuously.
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
