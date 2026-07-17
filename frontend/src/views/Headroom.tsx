import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { api, EnvelopeInstance, EnvelopeView, HeadroomInstance, HeadroomView } from "../api";

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

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], { hour12: false });
}

/** Fine-grained burst peaks and worst-case compositions from recorded
 * traffic, judged against the calibrated capacity limits: what a short
 * burst demands of this coordinator, not what the averages suggest. */
function EnvelopeBlock({ env }: { env: EnvelopeInstance }) {
  const sustained = env.limits?.sustained_eps ?? null;
  const ceiling = env.limits?.ceiling_eps ?? null;
  const pressured =
    env.burst_utilization_pct !== null && env.burst_utilization_pct >= 80;
  return (
    <>
      <div className="kv-grid">
        <span title="The single busiest second of transmit traffic recorded in the window, and the busiest 10 second stretch. Benchmark traffic is excluded.">
          Peak burst
        </span>
        <span>
          {env.peak
            ? `${env.peak.eps_1s}/s for 1 s at ${when(env.peak.at)} · ${env.peak.eps_10s}/s over 10 s`
            : "no traffic recorded in the window"}
          {env.coverage === "commands" && (
            <span
              className="chip"
              title="No wiretap covers this coordinator, so peaks come from observed MQTT commands; frames the controller sends on its own are not counted"
            >
              from commands only
            </span>
          )}
        </span>
        <span title="The peak 1 s burst as a share of the sustained capacity limit. A brief burst above the sustained limit queues for a moment and clears; only bursts above the hard ceiling (the highest rate the benchmark ever pushed through) stall the pipeline.">
          Burst vs capacity
        </span>
        <span>
          {env.burst_utilization_pct !== null && sustained !== null ? (
            <>
              <span className={pressured ? "chip warn" : "chip ok"}>
                {env.burst_utilization_pct}% of the sustained limit ({sustained}/s)
              </span>
              {ceiling !== null && ` · hard ceiling ${ceiling}/s`}
            </>
          ) : (
            "needs a calibrated capacity limit"
          )}
        </span>
        {env.composed_worst && (
          <>
            <span title="Automations seen bursting at the same moment, each priced at its own worst recorded burst: the load if their worst days land together. A composed burst above the hard ceiling would stall the pipeline if it ever landed in one second.">
              Worst composed burst
            </span>
            <span>
              {env.composed_worst.eps}/s if {env.composed_worst.commanders.join(" + ")}{" "}
              peak together (seen together at {when(env.composed_worst.observed_at)})
            </span>
          </>
        )}
      </div>
      {env.commanders.length > 0 && (
        <>
          <table className="table">
            <thead>
              <tr>
                <th title="Who sent the commands: the automation or MQTT client attribution names">
                  Commander
                </th>
                <th className="num" title="Command bursts recorded for this commander in the window">
                  Bursts
                </th>
                <th title="This commander's single heaviest recorded burst">Worst burst</th>
                <th className="num" title="Peak commands per second inside that burst">
                  Peak /s
                </th>
              </tr>
            </thead>
            <tbody>
              {env.commanders.slice(0, 5).map((entry) => (
                <tr key={entry.commander}>
                  <td>{entry.commander}</td>
                  <td className="num">{entry.bursts}</td>
                  <td>
                    {entry.worst.commands} commands in {entry.worst.duration_s} s at{" "}
                    {when(entry.worst.at)}
                  </td>
                  <td className="num">{entry.worst.peak_eps}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}

function InstancePanel({
  base,
  view,
  env,
}: {
  base: string;
  view: HeadroomInstance;
  env?: EnvelopeInstance;
}) {
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
              title={
                "The calibrated maximum sustainable command rate; ≥ marks a lower bound: the benchmark ended before anything degraded. " +
                (knee.mode === "spread"
                  ? "Measured across the whole coordinator (spread benchmark)."
                  : "Measured against a single device; the whole coordinator sustains more.")
              }
            >
              capacity limit {kneeLabel(view)}
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
      {env && (
        <>
          <p
            className="panel-kicker"
            title="What short bursts demand of this coordinator: recorded fine-grained peaks and worst-case compositions, not averages"
          >
            Burst envelope
          </p>
          <EnvelopeBlock env={env} />
        </>
      )}
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
  const [envelope, setEnvelope] = useState<EnvelopeView | null>(null);
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

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await api<EnvelopeView>(`/api/envelope?seconds=${seconds}`);
        if (alive) setEnvelope(data);
      } catch {
        // The headroom panels stand on their own without envelope data.
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 120000);
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
          <InstancePanel
            key={base}
            base={base}
            view={view.instances[base]}
            env={envelope?.instances[base]}
          />
        ))
      )}
      {envelope && envelope.fanouts.length > 0 && (
        <div className="panel">
          <p
            className="panel-kicker"
            title="Automations seen commanding several coordinators at the same moment. The combined rate is what one coordinator would have to absorb if those meshes were consolidated."
          >
            Cross-coordinator fan-outs
          </p>
          <table className="table">
            <thead>
              <tr>
                <th title="The automation or MQTT client the commands were attributed to">
                  Commander
                </th>
                <th title="Coordinators this commander was seen bursting on at the same moment, each with its worst recorded burst there">
                  Coordinators (worst burst each)
                </th>
                <th
                  className="num"
                  title="The sum of the per-coordinator worst bursts: the single-mesh load if these meshes were consolidated"
                >
                  Combined /s
                </th>
                <th title="When the concurrent bursts were recorded">Seen</th>
              </tr>
            </thead>
            <tbody>
              {envelope.fanouts.slice(0, 8).map((fanout) => (
                <tr key={fanout.commander}>
                  <td>{fanout.commander}</td>
                  <td>
                    {Object.entries(fanout.instances)
                      .map(([instance, eps]) => `${instance} (${eps}/s)`)
                      .join(" · ")}
                  </td>
                  <td className="num">{fanout.combined_eps}</td>
                  <td>{when(fanout.observed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            Read-only analysis of recorded traffic: nothing here transmits on the mesh.
            Combined rates assume each coordinator's worst burst lands at the same moment,
            which the recorded overlap shows has happened at least once.
          </p>
        </div>
      )}
    </>
  );
}
