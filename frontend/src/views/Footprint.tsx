import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, HaView, TapStats, TapView, Tile } from "../api";

const CAPABILITY_LABELS: Record<string, string> = {
  z2m_extension: "Z2M extension probe (runs inside Zigbee2MQTT)",
  topology_pull: "Topology pulls (active mesh scans)",
  mqtt_discovery: "HA entities via MQTT discovery (standing publisher)",
};

const GRANT_CAPABILITIES = new Set(["topology_pull", "mqtt_discovery"]);

function statusChip(tile: Tile): { label: string; className: string } {
  if (tile.status === "deployed" && tile.health === "stale") {
    return { label: "degraded — no heartbeat", className: "chip warn" };
  }
  if (tile.status === "deployed" && tile.drift) {
    return { label: `deployed — v${tile.probe.version} (update available)`, className: "chip warn" };
  }
  switch (tile.status) {
    case "deployed":
      return { label: `deployed v${tile.version ?? "?"}`, className: "chip ok" };
    case "deploying":
      return { label: "deploying…", className: "chip warn" };
    case "granted":
      return { label: "granted", className: "chip ok" };
    case "error":
      return { label: "error", className: "chip bad" };
    case "revoked":
      return { label: "revoked", className: "chip" };
    default:
      return {
        label: GRANT_CAPABILITIES.has(tile.capability) ? "not granted" : "not deployed",
        className: "chip",
      };
  }
}

function HaCard() {
  const [view, setView] = useState<HaView | null>(null);
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api<HaView>("/api/ha");
      setView(data);
      if (data.url) setUrl(data.url);
    } catch {
      /* card stays in loading state */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/ha", { method: "POST", body: JSON.stringify({ url, token }) });
      setToken("");
      setEditing(false);
      void refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to reach the collector");
    } finally {
      setBusy(false);
    }
  }

  const state = view?.status.state ?? "…";
  const counters = view?.status.counters;

  return (
    <div className="panel">
      <p className="panel-kicker">Home Assistant integration (per-automation attribution)</p>
      <p className="hint">
        A read-only WebSocket subscription resolves each <code>mqtt.publish</code> to the
        automation or script that fired it, naming commanders in the Attribution explorer.
        Broker-safe: no broker changes, no write access to HA. Create a long-lived token in
        HA (Profile → Security → Long-lived access tokens) and paste it here — it is stored
        in the collector's data volume and never displayed again.
      </p>
      {view?.configured && !editing ? (
        <p>
          <span className={state === "connected" ? "chip ok" : "chip warn"}>
            {state}
            {view.status.error ? ` — ${view.status.error}` : ""}
          </span>{" "}
          <span className="mono">{view.url}</span>
          {counters && (
            <span className="hint">
              {" "}
              · {counters.publishes ?? 0} publishes seen, {counters.named ?? 0} named
            </span>
          )}{" "}
          <button className="ghost small" onClick={() => setEditing(true)}>
            Reconfigure
          </button>
        </p>
      ) : (
        <form className="stack" onSubmit={(event) => void handleSubmit(event)}>
          <div className="row">
            <label className="grow">
              HA URL
              <input
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="http://homeassistant.local:8123"
                required
              />
            </label>
          </div>
          <label>
            Long-lived access token
            <input
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              autoComplete="off"
              required
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button type="submit" disabled={busy || !url || !token}>
            {busy ? "Testing connection…" : "Connect"}
          </button>
        </form>
      )}
    </div>
  );
}

function describeAgent(meta: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of ["agent", "host", "iface", "filter", "version"]) {
    const value = meta[key];
    if (typeof value === "string" && value) parts.push(`${key} ${value}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "(no hello metadata)";
}

function TapAgentsPanel({ tap }: { tap: TapStats | null }) {
  const now = Date.now() / 1000;
  const flows = tap?.flows ?? [];
  return (
    <div className="panel">
      <p className="panel-kicker">Wiretap agents</p>
      <p className="hint">
        A capture agent is a small process on a host that can see the coordinators' network
        traffic. It knows nothing about Zigbee: it streams a filtered packet capture
        outbound to this collector over a scoped token, and all decoding happens here.
        Uninstall on the capture host with <code>ninja-tap uninstall</code> (one-click
        removal from this page is planned).
      </p>
      {!tap || tap.agents === 0 ? (
        <p className="hint">No agents connected.</p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>Agent</th>
                <th className="num">Streamed</th>
                <th className="num">Segments</th>
              </tr>
            </thead>
            <tbody>
              {tap.agent_details.map((agent, index) => (
                <tr key={index}>
                  <td className="mono">{describeAgent(agent.meta)}</td>
                  <td className="num">{(agent.bytes / 1_000_000).toFixed(1)} MB</td>
                  <td className="num">{agent.segments}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            Coordinator flows:{" "}
            {flows.map((flow) => (
              <span
                key={flow.coordinator}
                className={now - flow.last_seen < 120 ? "chip ok" : "chip warn"}
              >
                {flow.instance ?? flow.coordinator}
              </span>
            ))}
          </p>
        </>
      )}
    </div>
  );
}

export default function Footprint() {
  const [tiles, setTiles] = useState<Tile[]>([]);
  const [tap, setTap] = useState<TapStats | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api<{ tiles: Tile[] }>("/api/tiles");
      setTiles(data.tiles);
      const tapView = await api<TapView>("/api/tap");
      setTap(tapView.stats);
    } catch {
      setError("Failed to load footprint");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function act(path: string, tile?: Tile) {
    setBusy(tile ? `${tile.capability}/${tile.target}` : "all");
    setError(null);
    try {
      await api(path, {
        method: "POST",
        body: tile
          ? JSON.stringify({ capability: tile.capability, target: tile.target })
          : undefined,
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed");
    } finally {
      setBusy(null);
      void refresh();
    }
  }

  const anyDeployed = tiles.some((tile) => ["deployed", "deploying", "error"].includes(tile.status));

  return (
    <>
      <HaCard />
      <div className="panel">
        <p className="panel-kicker">Every foothold, listed and revocable</p>
        <p className="hint">
          Nothing is deployed without a grant from this page, everything deployed is
          version-stamped and heartbeat-monitored, and every probe fails open — if
          zigbee-ninja dies, your mesh never notices. The extension probe installs and
          removes purely over MQTT; it reports payload sizes, never contents. Its heartbeat
          also self-reports which Z2M event hooks attached on your version.
        </p>
        {error && <p className="error">{error}</p>}
        <table className="table">
          <thead>
            <tr>
              <th>Capability</th>
              <th>Target</th>
              <th>Status</th>
              <th title="Which Zigbee2MQTT event hooks the extension probe attached on this instance's version — self-reported in its heartbeat">
                Hooks (self-reported)
              </th>
              <th
                className="num"
                title="Telemetry the probe had to drop under pressure, plus gaps in its sequence numbers"
              >
                Drops
              </th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tiles.map((tile) => {
              const chip = statusChip(tile);
              const key = `${tile.capability}/${tile.target}`;
              const counters = tile.probe.counters ?? {};
              return (
                <tr key={key}>
                  <td>{CAPABILITY_LABELS[tile.capability] ?? tile.capability}</td>
                  <td className="mono">{tile.target}</td>
                  <td>
                    <span className={chip.className}>{chip.label}</span>
                    {tile.detail && <div className="hint">{tile.detail}</div>}
                  </td>
                  <td className="hooks">
                    {tile.capability === "z2m_extension" && tile.probe.hooks.length > 0
                      ? tile.probe.hooks.join(", ")
                      : "—"}
                  </td>
                  <td className="num">
                    {tile.capability === "z2m_extension"
                      ? (counters.dropped ?? 0) + (tile.probe.seq_gaps ?? 0) || ""
                      : ""}
                  </td>
                  <td className="num">
                    {["available", "revoked", "error"].includes(tile.status) && (
                      <button
                        className="small"
                        disabled={busy !== null}
                        onClick={() => void act("/api/tiles/deploy", tile)}
                      >
                        {busy === key
                          ? "…"
                          : GRANT_CAPABILITIES.has(tile.capability)
                            ? "Grant"
                            : "Deploy"}
                      </button>
                    )}
                    {tile.capability === "z2m_extension" &&
                      tile.status === "deployed" &&
                      tile.drift && (
                        <button
                          className="small"
                          disabled={busy !== null}
                          title={`Replace the running extension with the bundled v${tile.bundled_version} in place — no revoke, the grant is untouched`}
                          onClick={() => void act("/api/tiles/deploy", tile)}
                        >
                          {busy === key ? "…" : `Update to v${tile.bundled_version}`}
                        </button>
                      )}
                    {["deployed", "deploying", "granted"].includes(tile.status) && (
                      <button
                        className="ghost small"
                        disabled={busy !== null}
                        onClick={() => void act("/api/tiles/revoke", tile)}
                      >
                        {busy === key ? "…" : "Revoke"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {tiles.length === 0 && (
              <tr>
                <td colSpan={6} className="hint">
                  No targets yet — tiles appear once discovery finds Zigbee2MQTT instances.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <TapAgentsPanel tap={tap} />
      {anyDeployed && (
        <div className="panel">
          <p className="panel-kicker">Emergency</p>
          <button
            className="ghost"
            disabled={busy !== null}
            onClick={() => void act("/api/tiles/revoke_all")}
          >
            {busy === "all" ? "Revoking…" : "Revoke everything zigbee-ninja has deployed"}
          </button>
        </div>
      )}
    </>
  );
}
