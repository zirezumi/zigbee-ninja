import { useCallback, useEffect, useState } from "react";
import { api, ApiError, InstanceInfo, Tile, TopologySummary, TopologyView } from "../api";

function age(pulledAt: number): string {
  const seconds = Math.max(0, Date.now() / 1000 - pulledAt);
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

interface InstancePanelProps {
  base: string;
  summary?: TopologySummary;
  granted: boolean;
  busy: boolean;
  onPull: () => void;
}

function InstancePanel({ base, summary, granted, busy, onPull }: InstancePanelProps) {
  return (
    <div className="panel">
      <div className="toolbar">
        <p className="panel-kicker">{base}</p>
        <span className="hint">
          {summary ? `snapshot ${age(summary.pulled_at)}` : "no snapshot yet"}
        </span>
        <button
          className="small"
          disabled={!granted || busy}
          title={granted ? "Run a networkmap sweep now" : "Grant topology pulls in Footprint first"}
          onClick={onPull}
        >
          {busy ? "Scanning…" : "Pull now"}
        </button>
      </div>
      {!summary ? (
        <p className="hint">
          {granted
            ? "Pull a snapshot to map this mesh."
            : "Topology pulls are not granted for this instance — grant them on the Footprint page."}
        </p>
      ) : (
        <>
          <p>
            {summary.node_count} nodes · {summary.link_count} links ·{" "}
            {Object.entries(summary.by_type)
              .map(([kind, count]) => `${count} ${kind.toLowerCase()}`)
              .join(" · ")}
            {summary.unresponsive_nodes.length > 0 && (
              <span className="chip bad">
                {" "}
                {summary.unresponsive_nodes.length} unanswered sweep
              </span>
            )}
          </p>
          {summary.unresponsive_nodes.length > 0 && (
            <p className="hint">
              No answer to the neighbor-table query (possibly unreachable):{" "}
              {summary.unresponsive_nodes.join(", ")}
            </p>
          )}
          {summary.query_failures.filter(
            (failure) => !summary.unresponsive_nodes.includes(failure.node),
          ).length > 0 && (
            <p className="hint">
              Answered the sweep but omitted{" "}
              {summary.query_failures
                .filter((failure) => !summary.unresponsive_nodes.includes(failure.node))
                .map((failure) => `${failure.node} (${failure.failed.join(", ")})`)
                .join(", ")}{" "}
              — a firmware omission on those devices, not a reachability problem.
            </p>
          )}
          <div className="panel-grid">
            <div>
              <p className="panel-kicker">Weakest links (LQI &lt; 80)</p>
              {summary.weak_links.length === 0 ? (
                <p className="hint">None — every reported link is at LQI 80 or better.</p>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>Link</th>
                      <th className="num">LQI</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.weak_links.map((link, index) => (
                      <tr key={index}>
                        <td>
                          {link.source} ↔ {link.target}
                        </td>
                        <td className="num">{link.lqi}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div>
              <p className="panel-kicker">Most-connected nodes</p>
              <table className="table">
                <thead>
                  <tr>
                    <th>Node</th>
                    <th className="num">Links</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.top_degree.map((row) => (
                    <tr key={row.node}>
                      <td>{row.node}</td>
                      <td className="num">{row.links}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export default function Topology() {
  const [instances, setInstances] = useState<InstanceInfo[]>([]);
  const [snapshots, setSnapshots] = useState<Record<string, TopologySummary>>({});
  const [grants, setGrants] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [instanceData, topologyData, tileData] = await Promise.all([
        api<{ instances: InstanceInfo[] }>("/api/instances"),
        api<TopologyView>("/api/topology"),
        api<{ tiles: Tile[] }>("/api/tiles"),
      ]);
      setInstances(instanceData.instances);
      setSnapshots(topologyData.instances);
      setGrants(
        Object.fromEntries(
          tileData.tiles
            .filter((tile) => tile.capability === "topology_pull")
            .map((tile) => [tile.target, tile.status === "granted"]),
        ),
      );
    } catch {
      setError("Failed to load topology data");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 30000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function pull(base: string) {
    setBusy(base);
    setError(null);
    try {
      await api("/api/topology/pull", {
        method: "POST",
        body: JSON.stringify({ capability: "topology_pull", target: base }),
      });
    } catch (err) {
      setError(err instanceof ApiError ? `${base}: ${err.message}` : "Pull failed");
    } finally {
      setBusy(null);
      void refresh();
    }
  }

  const bases = instances.map((instance) => instance.base_topic).sort();

  return (
    <>
      <div className="banner ok">
        <span>
          A pull asks Zigbee2MQTT to sweep the mesh (Mgmt_Lqi/Mgmt_Rtg to every router) —
          real mesh traffic, so it is grant-gated per instance and rate-limited to one scan
          per 15 minutes, one instance at a time.
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      {bases.length === 0 ? (
        <div className="panel">
          <p className="hint">No instances discovered yet.</p>
        </div>
      ) : (
        bases.map((base) => (
          <InstancePanel
            key={base}
            base={base}
            summary={snapshots[base]}
            granted={grants[base] ?? false}
            busy={busy === base}
            onPull={() => void pull(base)}
          />
        ))
      )}
    </>
  );
}
