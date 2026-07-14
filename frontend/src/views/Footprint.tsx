import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Tile } from "../api";

const CAPABILITY_LABELS: Record<string, string> = {
  z2m_extension: "Z2M extension probe (T1)",
};

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
    case "error":
      return { label: "error", className: "chip bad" };
    case "revoked":
      return { label: "revoked", className: "chip" };
    default:
      return { label: "not deployed", className: "chip" };
  }
}

export default function Footprint() {
  const [tiles, setTiles] = useState<Tile[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api<{ tiles: Tile[] }>("/api/tiles");
      setTiles(data.tiles);
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
              <th>Hooks (self-reported)</th>
              <th className="num">Drops</th>
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
                    {tile.probe.hooks.length > 0 ? tile.probe.hooks.join(", ") : "—"}
                  </td>
                  <td className="num">
                    {(counters.dropped ?? 0) + (tile.probe.seq_gaps ?? 0) || ""}
                  </td>
                  <td className="num">
                    {["available", "revoked", "error"].includes(tile.status) && (
                      <button
                        className="small"
                        disabled={busy !== null}
                        onClick={() => void act("/api/tiles/deploy", tile)}
                      >
                        {busy === key ? "…" : "Deploy"}
                      </button>
                    )}
                    {["deployed", "deploying"].includes(tile.status) && (
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
