import { useCallback, useEffect, useMemo, useState } from "react";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import {
  api,
  ApiError,
  InstanceInfo,
  Tile,
  TopologyGraph,
  TopologyGraphNode,
  TopologySummary,
  TopologyView,
} from "../api";

function age(pulledAt: number): string {
  const seconds = Math.max(0, Date.now() / 1000 - pulledAt);
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

const GRAPH_WIDTH = 860;
const GRAPH_HEIGHT = 480;
const WEAK_LQI = 80;

type SimNode = TopologyGraphNode & SimulationNodeDatum;
type SimLink = SimulationLinkDatum<SimNode> & { lqi: number | null };

function nodeRadius(node: TopologyGraphNode): number {
  if (node.type === "Coordinator") return 11;
  // Routers grow with how much the mesh leans on them: neighbor links plus
  // the routing-table paths observed flowing through them.
  if (node.type === "Router") {
    return 5 + Math.min(9, Math.sqrt(node.degree + 2 * node.routes_via) * 1.1);
  }
  return 3.5;
}

function nodeColor(node: TopologyGraphNode): string {
  if (node.type === "Coordinator") return "var(--accent)";
  if (node.type === "Router") return "color-mix(in srgb, var(--accent) 55%, var(--panel))";
  return "var(--ink-2)";
}

/** Force-directed mesh graph over the stored raw networkmap: LQI-weighted
 * edges (weak links dashed), routers sized by how many links and observed
 * routing paths lean on them. The simulation settles synchronously — small
 * meshes need no animation loop. */
function MeshGraph({ base, pulledAt }: { base: string; pulledAt: number }) {
  const [graphData, setGraphData] = useState<TopologyGraph | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await api<TopologyGraph>(
          `/api/topology/graph?instance=${encodeURIComponent(base)}`,
        );
        if (alive) setGraphData(data);
      } catch {
        if (alive) setGraphData(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [base, pulledAt]);

  const layout = useMemo(() => {
    if (!graphData || graphData.nodes.length === 0) return null;
    const nodes: SimNode[] = graphData.nodes.map((node) => ({ ...node }));
    const links: SimLink[] = graphData.links.map((link) => ({ ...link }));
    const simulation = forceSimulation(nodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((node) => node.id)
          // Better-heard neighbors sit closer; unknown LQI gets the midpoint.
          .distance((link) => 34 + (254 - (link.lqi ?? 127)) / 5)
          .strength(0.35),
      )
      .force("charge", forceManyBody().strength(-90))
      .force("center", forceCenter(GRAPH_WIDTH / 2, GRAPH_HEIGHT / 2))
      .force(
        "collide",
        forceCollide<SimNode>().radius((node) => nodeRadius(node) + 5),
      )
      .stop();
    simulation.tick(300);
    const labeled = new Set(
      nodes
        .filter((node) => node.type === "Router")
        .sort((a, b) => b.degree + 2 * b.routes_via - (a.degree + 2 * a.routes_via))
        .slice(0, 5)
        .map((node) => node.id),
    );
    return { nodes, links, labeled };
  }, [graphData]);

  if (!layout) return null;
  return (
    <>
      <svg
        className="mesh-graph"
        viewBox={`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`}
        role="img"
        aria-label={`Mesh graph for ${base}`}
      >
        {layout.links.map((link, index) => {
          const source = link.source as SimNode;
          const target = link.target as SimNode;
          const weak = link.lqi !== null && link.lqi < WEAK_LQI;
          return (
            <line
              key={index}
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke={weak ? "var(--danger)" : "var(--ink-2)"}
              strokeOpacity={0.2 + 0.55 * ((link.lqi ?? 100) / 254)}
              strokeWidth={weak ? 1.4 : 1}
              strokeDasharray={weak ? "4 3" : undefined}
            >
              <title>
                {source.name} ↔ {target.name}
                {link.lqi !== null ? ` · LQI ${link.lqi} (worse direction)` : ""}
              </title>
            </line>
          );
        })}
        {layout.nodes.map((node) => (
          <g key={node.id}>
            <circle
              cx={node.x}
              cy={node.y}
              r={nodeRadius(node)}
              fill={nodeColor(node)}
              stroke={node.failed ? "var(--danger)" : "var(--line)"}
              strokeWidth={node.failed ? 2 : 1}
            >
              <title>
                {node.name} · {node.type}
                {` · ${node.degree} link${node.degree === 1 ? "" : "s"}`}
                {node.routes_via > 0
                  ? ` · ${node.routes_via} routed path${node.routes_via === 1 ? "" : "s"} through it`
                  : ""}
              </title>
            </circle>
            {(node.type === "Coordinator" || layout.labeled.has(node.id)) && (
              <text
                x={node.x}
                y={(node.y ?? 0) - nodeRadius(node) - 4}
                textAnchor="middle"
                className="mesh-label"
              >
                {node.name}
              </text>
            )}
          </g>
        ))}
      </svg>
      <p className="hint">
        Hover a node or link for detail. Node size = how much the mesh leans on it
        (neighbor links + routing paths observed through it); line strength = link
        quality, with links under LQI {WEAK_LQI} dashed red. Labels mark the coordinator
        and the five most-leaned-on routers.
      </p>
    </>
  );
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
          <MeshGraph base={base} pulledAt={summary.pulled_at} />
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
