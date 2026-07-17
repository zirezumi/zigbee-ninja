import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, RuntimeSettings, TapView } from "../api";

function RetentionPanel({
  settings,
  onSaved,
}: {
  settings: RuntimeSettings;
  onSaved: () => void;
}) {
  const [rollupDays, setRollupDays] = useState(String(settings.retention_rollup_days));
  const [chainHours, setChainHours] = useState(String(settings.retention_chains_hours));
  const [topoKept, setTopoKept] = useState(String(settings.retention_topology_snapshots));
  const [eventQuota, setEventQuota] = useState(String(settings.raw_event_quota_mb));
  const [eventHorizon, setEventHorizon] = useState(
    String(settings.raw_event_horizon_hours),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          retention_rollup_days: Number(rollupDays),
          retention_chains_hours: Number(chainHours),
          retention_topology_snapshots: Number(topoKept),
          raw_event_quota_mb: Number(eventQuota),
          raw_event_horizon_hours: Number(eventHorizon),
        }),
      });
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to reach the collector");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <p className="panel-kicker">Retention</p>
      <p className="hint">
        The 10 s rollup tiers (rates, attribution, airtime, latency) prune on the rollup
        window; command chains keep event-level detail for the chain window; topology
        snapshots keep the most recent N per instance. Alert event history is fixed at 90
        days. Values are clamped to sane ranges by the collector.
      </p>
      <form className="stack" onSubmit={(event) => void handleSubmit(event)}>
        <div className="row">
          <label title="How long the 10-second series (message rates, attribution classes, airtime, latency) are kept">
            Rollups (days)
            <input
              type="number"
              min="1"
              max="365"
              value={rollupDays}
              onChange={(event) => setRollupDays(event.target.value)}
              required
            />
          </label>
          <label title="How long individual command chains are kept; the daily cost ledger and rollups persist beyond this">
            Chains (hours)
            <input
              type="number"
              min="1"
              max="720"
              value={chainHours}
              onChange={(event) => setChainHours(event.target.value)}
              required
            />
          </label>
          <label title="How many stored network scans to keep per coordinator">
            Topology snapshots
            <input
              type="number"
              min="1"
              max="200"
              value={topoKept}
              onChange={(event) => setTopoKept(event.target.value)}
              required
            />
          </label>
          <label title="Disk cap for the Benchmark view's raw event store; once hit, the oldest hours are dropped first">
            Raw events (MB)
            <input
              type="number"
              min="64"
              max="65536"
              value={eventQuota}
              onChange={(event) => setEventQuota(event.target.value)}
              required
            />
          </label>
          <label title="How far back the raw event store reaches, within the disk cap">
            Raw events (hours)
            <input
              type="number"
              min="1"
              max="720"
              value={eventHorizon}
              onChange={(event) => setEventHorizon(event.target.value)}
              required
            />
          </label>
        </div>
        {error && <p className="error">{error}</p>}
        <div className="row">
          <button type="submit" disabled={busy}>
            {busy ? "…" : "Save retention"}
          </button>
          {saved && <span className="chip ok">saved</span>}
        </div>
      </form>
    </div>
  );
}

function LabelsPanel({
  settings,
  onSaved,
}: {
  settings: RuntimeSettings;
  onSaved: () => void;
}) {
  const [labels, setLabels] = useState<Array<[string, string]>>(() =>
    Object.entries(settings.client_labels),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function setEntry(index: number, key: string, value: string) {
    setLabels((current) =>
      current.map((entry, i) => (i === index ? [key, value] : entry)),
    );
  }

  async function save(next: Array<[string, string]>) {
    setBusy(true);
    setError(null);
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ client_labels: Object.fromEntries(next) }),
      });
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to reach the collector");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <p className="panel-kicker">Client labels</p>
      <p className="hint">
        Friendly names for MQTT client ids in the Attribution explorer (e.g.{" "}
        <code>ha-core</code> → “Home Assistant”). Rows with an empty id or label are
        dropped on save.
      </p>
      {labels.map(([client, label], index) => (
        <div className="row" key={index}>
          <label className="grow">
            Client id
            <input
              value={client}
              onChange={(event) => setEntry(index, event.target.value, label)}
            />
          </label>
          <label className="grow">
            Label
            <input
              value={label}
              onChange={(event) => setEntry(index, client, event.target.value)}
            />
          </label>
          <button
            type="button"
            className="ghost small"
            disabled={busy}
            onClick={() => {
              const next = labels.filter((_, i) => i !== index);
              setLabels(next);
              void save(next);
            }}
          >
            Remove
          </button>
        </div>
      ))}
      {error && <p className="error">{error}</p>}
      <div className="row">
        <button
          type="button"
          className="small"
          disabled={busy}
          onClick={() => setLabels((current) => [...current, ["", ""]])}
        >
          Add label
        </button>
        <button type="button" disabled={busy} onClick={() => void save(labels)}>
          {busy ? "…" : "Save labels"}
        </button>
      </div>
    </div>
  );
}

function TapTokenPanel() {
  const [tap, setTap] = useState<TapView | null>(null);
  const [revealed, setRevealed] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setTap(await api<TapView>("/api/tap"));
      } catch {
        /* panel stays hidden */
      }
    })();
  }, []);

  if (tap === null) return null;
  return (
    <div className="panel">
      <p className="panel-kicker">Wiretap daemon token</p>
      <p className="hint">
        A ninja-tap capture daemon authenticates its outbound stream with this
        collector-scoped token. Treat it like a password: anyone holding it can stream
        capture data here.
      </p>
      <p>
        {revealed ? (
          <span className="mono">{tap.token}</span>
        ) : (
          <button className="ghost small" onClick={() => setRevealed(true)}>
            Reveal token
          </button>
        )}
      </p>
    </div>
  );
}

export default function Settings() {
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSettings(await api<RuntimeSettings>("/api/settings"));
    } catch {
      /* keep the previous view */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (settings === null) {
    return <p className="hint">loading…</p>;
  }

  return (
    <>
      <RetentionPanel settings={settings} onSaved={() => void refresh()} />
      <LabelsPanel
        key={JSON.stringify(settings.client_labels)}
        settings={settings}
        onSaved={() => void refresh()}
      />
      <TapTokenPanel />
    </>
  );
}
