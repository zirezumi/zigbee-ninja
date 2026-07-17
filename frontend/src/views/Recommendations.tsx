import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Recommendation, RecommendationsView } from "../api";

// Detector identifiers stay stable in the API; the GUI translates them.
const DETECTOR_LABELS: Record<string, string> = {
  pacing: "Command pacing",
  groupcast_economics: "Group economics",
  redundancy: "Duplicate commands",
  reporting: "Device reporting",
  rebalancing: "Coordinator rebalancing",
};

const DETECTOR_HINTS: Record<string, string> = {
  pacing:
    "Bursts of commands that push a coordinator toward its measured capacity limit, or one device past its service ceiling",
  groupcast_economics:
    "Places where switching between group commands and per-device commands would cost less airtime on this mesh",
  redundancy: "Identical commands resent within seconds; dropping them changes nothing",
  reporting: "Devices whose reporting costs far more airtime than comparable devices",
  rebalancing:
    "Move sets that would relieve a coordinator whose recorded command bursts cross its measured capacity limit, priced by the what-if scenario engine",
};

const STATE_TABS: Array<[string, string]> = [
  ["open", "Open"],
  ["dismissed", "Dismissed"],
  ["applied", "Applied"],
  ["all", "All"],
];

function when(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

/** Copy text to the clipboard. The async Clipboard API needs a secure
 * context, and installations reached over plain http (a LAN IP) do not
 * have one; the hidden-textarea path is the working fallback there. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the textarea path
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  document.body.removeChild(area);
  return copied;
}

/** The record a per-card Copy places on the clipboard: the full API
 * record plus the human detector label. */
function copyPayload(rec: Recommendation): string {
  return JSON.stringify(
    { detector_label: DETECTOR_LABELS[rec.detector] ?? rec.detector, ...rec },
    null,
    2,
  );
}

function savingLine(rec: Recommendation): string {
  const saving = rec.saving || {};
  if (saving.us_per_s && saving.us_per_s > 0) {
    const pct = saving.pct_of_budget ? ` (${saving.pct_of_budget}% of the channel budget)` : "";
    return `saves about ${Math.round(saving.us_per_s)} µs/s of airtime${pct}`;
  }
  if (saving.p95_ms && saving.p95_ms > 0) {
    return `about ${Math.round(saving.p95_ms)} ms lower p95 latency during bursts`;
  }
  return "no airtime saving; see the finding";
}

/** One evidence entry as a readable line; unknown shapes fall back to pairs. */
function evidenceLine(entry: Record<string, unknown>): string {
  const kind = entry.kind as string | undefined;
  if (kind === "window") {
    return (
      `Burst at ${when(entry.start as number)}: ${entry.commands} commands, ` +
      `peak ${Math.round(entry.peak_eps as number)}/s (view the moment in Benchmark)`
    );
  }
  if (kind === "capacity_limit") {
    const stale = entry.stale_environment
      ? "; firmware changed since it was measured"
      : "";
    return (
      `Capacity limit ${entry.eps}/s (${entry.mode} benchmark, ` +
      `measured ${when(entry.measured_at as number)}${stale})`
    );
  }
  if (kind === "pricing") {
    return (
      `One command on this mesh: about ${Math.round(entry.unicast_us as number)} µs ` +
      `per device vs ${Math.round(entry.groupcast_us as number)} µs as a group ` +
      `(${entry.routers} routers relay each group command` +
      `${entry.avg_tx_measured ? `, measured ${entry.avg_tx}x retransmissions` : ""})`
    );
  }
  if (kind === "duplicates") {
    const targets = (entry.top_targets as Array<{ target: string; count: number }>) || [];
    const list = targets.map((t) => `${t.target} (${t.count})`).join(", ");
    return `${entry.count} duplicates, most often to: ${list}`;
  }
  if (kind === "ledger") {
    const versus =
      entry.compared_to === "peers"
        ? `median of ${entry.peers} same-model devices: ${entry.peer_median_us_per_s} µs/s`
        : `installation median: ${entry.fleet_median_us_per_s} µs/s`;
    return `${entry.publishes} reports costing ${entry.us_per_s} µs/s; ${versus}`;
  }
  if (kind === "group") {
    return `Group of ${entry.members} devices, commanded ${entry.commands} times`;
  }
  if (kind === "cofire") {
    return (
      `${entry.matched} of ${entry.total} commands to ${entry.inner} arrived within ` +
      `${entry.window_s} s of an identical command to ${entry.outer}`
    );
  }
  return Object.entries(entry)
    .filter(([key]) => key !== "kind")
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join(" · ");
}

function ConfidenceChip({ level }: { level: string }) {
  const title =
    "How solid the evidence is: high rides measured limits and recorded traffic; " +
    "medium and low carry stated caveats in the finding text";
  const className = level === "high" ? "chip ok" : "chip";
  return (
    <span className={className} title={title}>
      {level} confidence
    </span>
  );
}

function Card({
  rec,
  onState,
}: {
  rec: Recommendation;
  onState: (rec: Recommendation, state: string) => void;
}) {
  const [copyLabel, setCopyLabel] = useState("Copy");

  async function copyCard() {
    const copied = await copyText(copyPayload(rec));
    setCopyLabel(copied ? "Copied" : "Copy failed");
    window.setTimeout(() => setCopyLabel("Copy"), 1500);
  }

  return (
    <div className="panel">
      <div className="toolbar">
        <p className="panel-kicker" title={DETECTOR_HINTS[rec.detector]}>
          {DETECTOR_LABELS[rec.detector] ?? rec.detector} · {rec.instance}
        </p>
        <ConfidenceChip level={rec.confidence} />
        {rec.state !== "open" && <span className="chip">{rec.state}</span>}
        <span className="chip" title={rec.saving.basis ?? undefined}>
          {savingLine(rec)}
        </span>
      </div>
      <p>{rec.finding}</p>
      {rec.state_note && <p className="hint">Note: {rec.state_note}</p>}
      <details>
        <summary className="hint">
          Evidence ({rec.evidence.length}) · first seen {when(rec.created_at)} · last
          confirmed {when(rec.updated_at)}
          {rec.saving.basis ? ` · ${rec.saving.basis}` : ""}
        </summary>
        <ul>
          {rec.evidence.map((entry, index) => (
            <li key={index} className="hint">
              {evidenceLine(entry)}
            </li>
          ))}
        </ul>
      </details>
      <div className="toolbar">
        {rec.state === "open" && (
          <>
            <button className="ghost" onClick={() => onState(rec, "dismissed")}>
              Dismiss
            </button>
            <button
              className="ghost"
              title="Tell zigbee-ninja you changed the installation as suggested; V2 verification will measure the before/after"
              onClick={() => onState(rec, "applied")}
            >
              Mark applied
            </button>
          </>
        )}
        {(rec.state === "dismissed" || rec.state === "applied") && (
          <button className="ghost" onClick={() => onState(rec, "open")}>
            Reopen
          </button>
        )}
        <button
          className="ghost"
          title="Copy this recommendation as JSON: detector, coordinator, finding text, saving, and evidence"
          onClick={() => void copyCard()}
        >
          {copyLabel}
        </button>
      </div>
    </div>
  );
}

export default function Recommendations() {
  const [stateFilter, setStateFilter] = useState("open");
  const [view, setView] = useState<RecommendationsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api<RecommendationsView>(
        `/api/recommendations?state=${stateFilter}`,
      );
      setView(data);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load recommendations");
    }
  }, [stateFilter]);

  useEffect(() => {
    void load();
    const interval = window.setInterval(() => void load(), 60000);
    return () => window.clearInterval(interval);
  }, [load]);

  async function scanNow() {
    setScanning(true);
    try {
      await api("/api/recommendations/run", { method: "POST" });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Scan failed");
    } finally {
      setScanning(false);
    }
  }

  function exportJson() {
    if (!view || view.recommendations.length === 0) return;
    const blob = new Blob([JSON.stringify(view.recommendations, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `recommendations-${stateFilter}-${new Date()
      .toISOString()
      .slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function setState(rec: Recommendation, state: string) {
    let note: string | null = null;
    if (state === "dismissed") {
      note = window.prompt(
        "Optional note on why this is fine as it is (kept with the dismissal):",
        "",
      );
      if (note === null) return; // cancelled
    }
    try {
      await api(`/api/recommendations/${rec.id}/state`, {
        method: "POST",
        body: JSON.stringify({ state, note: note || null }),
      });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Update failed");
    }
  }

  const counts = view?.counts.by_state;
  const run = view?.run;
  const detectorsLine = run?.detectors
    .map((name) => DETECTOR_LABELS[name] ?? name)
    .join(", ");

  return (
    <>
      <div className="toolbar">
        <div className="segmented">
          {STATE_TABS.map(([value, label]) => (
            <button
              key={value}
              className={value === stateFilter ? "seg-btn active" : "seg-btn"}
              onClick={() => setStateFilter(value)}
            >
              {label}
              {counts && value !== "all" ? ` (${counts[value] ?? 0})` : ""}
            </button>
          ))}
        </div>
        <button className="ghost" onClick={() => void scanNow()} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan now"}
        </button>
        <button
          className="ghost"
          onClick={exportJson}
          disabled={!view || view.recommendations.length === 0}
          title="Download every recommendation the current tab shows as a JSON file"
        >
          Export JSON
        </button>
        <span
          className="hint"
          title={
            detectorsLine
              ? `Detectors: ${detectorsLine}. Each reads the recorded traffic stores; nothing here ever transmits on the mesh.`
              : undefined
          }
        >
          {run?.last_run_at
            ? `Last scan ${when(run.last_run_at)}; scans repeat hourly.`
            : "First scan runs a few minutes after startup."}
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {view === null ? (
        <p className="hint">loading…</p>
      ) : view.recommendations.length === 0 ? (
        <div className="panel">
          {stateFilter === "open" ? (
            <>
              <p>Nothing left that the evidence supports changing.</p>
              <p className="hint">
                Every detector ran against the recorded traffic and found no change worth
                proposing: with budgets green, this installation is provably
                traffic-optimized for the recorded window. New findings appear here as
                traffic patterns change.
              </p>
            </>
          ) : (
            <p className="hint">No {stateFilter === "all" ? "" : stateFilter + " "}recommendations.</p>
          )}
        </div>
      ) : (
        view.recommendations.map((rec) => (
          <Card key={rec.id} rec={rec} onState={(r, s) => void setState(r, s)} />
        ))
      )}
      <p className="hint">
        Recommendations are ordered by saving times confidence. Savings are comparable
        estimates in the same currency as Top spenders, not meter readings; each card's
        evidence says exactly what was measured and what was modeled. Applying a change
        is always your tooling's job: zigbee-ninja never writes to the mesh, the broker,
        or the controller.
      </p>
    </>
  );
}
