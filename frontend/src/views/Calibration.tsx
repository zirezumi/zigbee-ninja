import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  CalibrationActive,
  CalibrationCandidate,
  CalibrationPreview,
  CalibrationRecord,
  CalibrationStep,
  CalibrationView,
  CandidatesView,
  InstanceInfo,
} from "../api";

function ago(ts: number): string {
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 48 * 3600) return `${(seconds / 3600).toFixed(1)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function stepRtt(step: CalibrationStep): number | null {
  return step.rtt_source === "wire" ? step.wire_p95_ms : step.echo_p95_ms;
}

/** p95 RTT vs ramp rate, no chart dependency: geometric rates plot on an
 * index axis with the rate as the tick label. */
function RttCurve({ steps }: { steps: CalibrationStep[] }) {
  const points = steps
    .map((step) => ({ rate: step.rate_eps, rtt: stepRtt(step), breach: step.breach }))
    .filter((point) => point.rtt !== null) as Array<{
    rate: number;
    rtt: number;
    breach: string | null;
  }>;
  if (points.length < 2) return null;
  const width = 420;
  const height = 130;
  const pad = { left: 44, right: 10, top: 10, bottom: 22 };
  const maxRtt = Math.max(...points.map((point) => point.rtt));
  const x = (index: number) =>
    pad.left + (index * (width - pad.left - pad.right)) / Math.max(points.length - 1, 1);
  const y = (rtt: number) =>
    height - pad.bottom - (rtt / maxRtt) * (height - pad.top - pad.bottom);
  const path = points
    .map((point, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(point.rtt)}`)
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      style={{ width: "100%", maxWidth: width }}
      role="img"
      aria-label="p95 RTT vs ramp rate"
    >
      <text x={4} y={pad.top + 8} fontSize="10" fill="currentColor" opacity="0.6">
        {Math.round(maxRtt)} ms
      </text>
      <line
        x1={pad.left}
        y1={height - pad.bottom}
        x2={width - pad.right}
        y2={height - pad.bottom}
        stroke="currentColor"
        opacity="0.25"
      />
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.85" />
      {points.map((point, index) => (
        <g key={index}>
          <circle
            cx={x(index)}
            cy={y(point.rtt)}
            r="3"
            fill={point.breach ? "var(--bad, #e5484d)" : "currentColor"}
          />
          <text
            x={x(index)}
            y={height - 8}
            fontSize="10"
            textAnchor="middle"
            fill="currentColor"
            opacity="0.6"
          >
            {point.rate}/s
          </text>
        </g>
      ))}
    </svg>
  );
}

function StepsTable({ steps }: { steps: CalibrationStep[] }) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th className="num">Rate /s</th>
          <th className="num">Sent</th>
          <th className="num">Replies</th>
          <th className="num">Timeouts</th>
          <th className="num">Achieved /s</th>
          <th className="num">p95 RTT ms</th>
          <th>Source</th>
          <th>Stopped by</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((step, index) => (
          <tr key={index}>
            <td className="num">{step.rate_eps}</td>
            <td className="num">{step.sent}</td>
            <td className="num">{step.completed}</td>
            <td className="num">{step.timeouts}</td>
            <td className="num">{step.achieved_eps}</td>
            <td className="num">{stepRtt(step) ?? "—"}</td>
            <td>{step.rtt_source ?? "—"}</td>
            <td>{step.breach ? <span className="chip bad">{step.breach}</span> : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ActivePanel({ active, onAbort }: { active: CalibrationActive; onAbort: () => void }) {
  const rows = active.current ? [...active.steps, active.current] : active.steps;
  return (
    <div className="panel">
      <div className="toolbar">
        <p className="panel-kicker">
          Calibrating {active.target} on {active.instance}
        </p>
        <span className="chip warn">
          step {Math.min(active.step_index + 1, active.total_steps)}/{active.total_steps} ·{" "}
          {active.state}
        </span>
        <span className="hint">
          {active.sent_total} reads sent · {active.outstanding} in flight
        </span>
        <button className="small" onClick={onAbort}>
          Abort now
        </button>
      </div>
      {active.abort_requested && (
        <p className="error">Abort requested: {active.abort_requested}</p>
      )}
      <RttCurve steps={rows} />
      <StepsTable steps={rows} />
    </div>
  );
}

function PreviewPanel({
  preview,
  busy,
  onAuthorize,
  onCancel,
}: {
  preview: CalibrationPreview;
  busy: boolean;
  onAuthorize: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="panel">
      <div className="toolbar">
        <p className="panel-kicker">
          Dry run — calibrate {preview.target} on {preview.instance}
        </p>
        <span className="chip">{preview.rtt_source === "wire" ? "wire RTT" : "echo RTT"}</span>
      </div>
      <p>{preview.traffic}</p>
      <p className="mono hint">
        {preview.topic} ← {preview.payload}
      </p>
      <div className="panel-grid">
        <div>
          <p className="panel-kicker">Schedule</p>
          <table className="table">
            <thead>
              <tr>
                <th className="num">Rate /s</th>
                <th className="num">Duration s</th>
                <th className="num">Reads</th>
              </tr>
            </thead>
            <tbody>
              {preview.steps.map((step) => (
                <tr key={step.rate_eps}>
                  <td className="num">{step.rate_eps}</td>
                  <td className="num">{step.duration_s}</td>
                  <td className="num">{step.reads}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            {preview.total_reads} reads total · ≈{Math.round(preview.estimated_duration_s / 60)}{" "}
            min · then a {preview.cooldown_seconds}s cooldown
          </p>
        </div>
        <div>
          <p className="panel-kicker">Stops &amp; rails</p>
          <ul className="hint">
            <li>{String(preview.stop_rules.rtt_p95)}</li>
            <li>
              read timeout {preview.read_timeout_s}s; stop above{" "}
              {Math.round(Number(preview.stop_rules.timeout_ratio) * 100)}% timeouts
            </li>
            <li>stop when the closed loop can't reach the requested rate</li>
            <li>{preview.max_outstanding_rule}</li>
            <li>abort: {String(preview.watchdog.uninvolved_offline)}</li>
            <li>
              abort: ≥{String(preview.watchdog.bridge_error_lines)} Z2M error log lines;{" "}
              {String(preview.watchdog.stall)}
            </li>
            <li>
              hard caps: {preview.caps.max_rate_eps}/s, {preview.caps.max_total_reads} reads,{" "}
              {Math.round(preview.caps.max_run_seconds / 60)} min
            </li>
          </ul>
        </div>
      </div>
      {preview.warnings.map((warning) => (
        <p key={warning} className="hint">
          <span className="chip warn">note</span> {warning}
        </p>
      ))}
      <div className="toolbar">
        <button onClick={onAuthorize} disabled={busy}>
          {busy ? "Starting…" : "Authorize this run"}
        </button>
        <button className="small" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <span className="hint">
          Authorization is single-use and applies to this run only — nothing persists.
        </span>
      </div>
    </div>
  );
}

function CandidatesPanel({
  view,
  busyTarget,
  onSelect,
}: {
  view: CandidatesView;
  busyTarget: string | null;
  onSelect: (target: string) => void;
}) {
  return (
    <div className="panel">
      <div className="toolbar">
        <p className="panel-kicker">Pick a target on {view.instance}</p>
        <span className="hint">
          {view.topology_pulled_at
            ? `ranked with the topology snapshot from ${ago(view.topology_pulled_at)}`
            : "no topology snapshot — pull one for LQI-aware ranking"}
        </span>
      </div>
      {view.candidates.length === 0 ? (
        <p className="hint">No routers discovered on this instance.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Router</th>
              <th>Model</th>
              <th className="num">LQI</th>
              <th className="num">Links</th>
              <th className="num">Bindings</th>
              <th className="num">Groups</th>
              <th>Read</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {view.candidates.map((candidate: CalibrationCandidate) => (
              <tr key={candidate.friendly_name}>
                <td>
                  {candidate.friendly_name}
                  {candidate.reasons.length > 0 && (
                    <span className="hint"> — {candidate.reasons.join("; ")}</span>
                  )}
                </td>
                <td className="hint">
                  {candidate.vendor} {candidate.model}
                </td>
                <td className="num">{candidate.lqi ?? "—"}</td>
                <td className="num">{candidate.degree}</td>
                <td className="num">{candidate.binding_count}</td>
                <td className="num">{candidate.group_count}</td>
                <td className="mono">{candidate.get_attribute ?? "—"}</td>
                <td>
                  <button
                    className="small"
                    disabled={!candidate.eligible || busyTarget !== null}
                    onClick={() => onSelect(candidate.friendly_name)}
                  >
                    {busyTarget === candidate.friendly_name ? "…" : "Preview run"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function HistoryPanel({
  history,
  instances,
}: {
  history: CalibrationRecord[];
  instances: InstanceInfo[];
}) {
  const current = Object.fromEntries(
    instances.map((instance) => [
      instance.base_topic,
      `${instance.version ?? "?"}/${instance.coordinator_revision ?? "?"}`,
    ]),
  );
  return (
    <div className="panel">
      <p className="panel-kicker">History</p>
      {history.length === 0 ? (
        <p className="hint">No calibrations yet.</p>
      ) : (
        history.map((record) => {
          const environment = `${record.environment.z2m_version ?? "?"}/${
            record.environment.coordinator_revision ?? "?"
          }`;
          const drifted =
            current[record.instance] !== undefined && current[record.instance] !== environment;
          return (
            <details key={record.id}>
              <summary>
                {ago(record.started_at)} · {record.instance} · {record.target} ·{" "}
                {record.status === "completed" ? (
                  record.knee ? (
                    <strong>
                      knee {record.knee.censored ? "≥" : ""}
                      {record.knee.eps}/s
                    </strong>
                  ) : (
                    <span className="chip warn">no knee determined</span>
                  )
                ) : (
                  <span className="chip bad">{record.status}</span>
                )}{" "}
                {record.knee?.breach && <span className="hint">({record.knee.breach})</span>}
                {drifted && (
                  <span className="chip warn" title={`ran on ${environment}, now ${current[record.instance]}`}>
                    environment changed — recalibrate?
                  </span>
                )}
              </summary>
              {record.abort_reason && <p className="error">{record.abort_reason}</p>}
              <RttCurve steps={record.steps} />
              <StepsTable steps={record.steps} />
            </details>
          );
        })
      )}
    </div>
  );
}

export default function Calibration() {
  const [instances, setInstances] = useState<InstanceInfo[]>([]);
  const [view, setView] = useState<CalibrationView | null>(null);
  const [instance, setInstance] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidatesView | null>(null);
  const [preview, setPreview] = useState<CalibrationPreview | null>(null);
  const [busyTarget, setBusyTarget] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [instanceData, calibrationData] = await Promise.all([
        api<{ instances: InstanceInfo[] }>("/api/instances"),
        api<CalibrationView>("/api/calibration"),
      ]);
      setInstances(instanceData.instances);
      setView(calibrationData);
    } catch {
      setError("Failed to load calibration state");
    }
  }, []);

  const active = view?.active ?? null;

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), active ? 2000 : 15000);
    return () => window.clearInterval(interval);
  }, [refresh, active !== null]);

  async function loadCandidates(base: string) {
    setInstance(base);
    setPreview(null);
    setError(null);
    try {
      setCandidates(
        await api<CandidatesView>(
          `/api/calibration/candidates?instance=${encodeURIComponent(base)}`,
        ),
      );
    } catch (err) {
      setCandidates(null);
      setError(err instanceof ApiError ? err.message : "Failed to load candidates");
    }
  }

  async function loadPreview(target: string) {
    if (!instance) return;
    setBusyTarget(target);
    setError(null);
    try {
      setPreview(
        await api<CalibrationPreview>("/api/calibration/preview", {
          method: "POST",
          body: JSON.stringify({ instance, target }),
        }),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Preview failed");
    } finally {
      setBusyTarget(null);
    }
  }

  async function authorize() {
    if (!preview) return;
    setStarting(true);
    setError(null);
    try {
      await api("/api/calibration/run", {
        method: "POST",
        body: JSON.stringify({
          instance: preview.instance,
          target: preview.target,
          authorization: preview.authorization,
        }),
      });
      setPreview(null);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Run refused");
    } finally {
      setStarting(false);
    }
  }

  async function abort() {
    try {
      await api("/api/calibration/abort", { method: "POST" });
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Abort failed");
    }
  }

  const bases = instances.map((item) => item.base_topic).sort();
  const cooldown = view?.cooldown_until ?? null;

  return (
    <>
      <div className="banner ok">
        <span>
          A calibration run transmits on purpose: benign unicast reads of one router, ramped
          until the latency knee shows. Every run is individually authorized from its dry-run
          preview — there is no standing grant — and an abort control stays live throughout.
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {active ? (
        <ActivePanel active={active} onAbort={() => void abort()} />
      ) : (
        <>
          {cooldown && (
            <p className="hint">
              <span className="chip warn">cooldown</span> next run allowed in{" "}
              {Math.max(0, Math.round(cooldown - Date.now() / 1000))}s
            </p>
          )}
          <div className="panel">
            <div className="toolbar">
              <p className="panel-kicker">Coordinator</p>
              <div className="segmented">
                {bases.map((base) => (
                  <button
                    key={base}
                    className={base === instance ? "seg-btn active" : "seg-btn"}
                    onClick={() => void loadCandidates(base)}
                  >
                    {base}
                  </button>
                ))}
              </div>
            </div>
            {!instance && <p className="hint">Pick the coordinator to calibrate.</p>}
          </div>
          {preview ? (
            <PreviewPanel
              preview={preview}
              busy={starting}
              onAuthorize={() => void authorize()}
              onCancel={() => setPreview(null)}
            />
          ) : (
            candidates && (
              <CandidatesPanel
                view={candidates}
                busyTarget={busyTarget}
                onSelect={(target) => void loadPreview(target)}
              />
            )
          )}
        </>
      )}
      {view && <HistoryPanel history={view.history} instances={instances} />}
    </>
  );
}
