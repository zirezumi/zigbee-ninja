import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  ActiveAlert,
  AlertEvent,
  AlertMetricInfo,
  AlertRule,
  AlertsView,
  api,
  ApiError,
} from "../api";

const SEVERITIES = ["info", "warning", "critical"];
const HISTORY_WINDOWS: Array<{ label: string; seconds: number }> = [
  { label: "24 h", seconds: 86400 },
  { label: "7 d", seconds: 7 * 86400 },
  { label: "30 d", seconds: 30 * 86400 },
];

export function severityChip(severity: string | null | undefined): string {
  if (severity === "critical") return "chip bad";
  if (severity === "warning") return "chip warn";
  return "chip";
}

function formatTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function formatSince(ts: number | null | undefined): string {
  if (!ts) return "—";
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 129600) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function formatValue(value: number | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined) return "—";
  const rounded = Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
  return unit && unit !== "0/1" ? `${rounded} ${unit}` : rounded;
}

function conditionLabel(rule: AlertRule): string {
  let label = `${rule.metric} ${rule.op} ${rule.threshold}`;
  if (rule.clear_threshold !== null) {
    label += ` (clear ${rule.op === ">" ? "≤" : "≥"} ${rule.clear_threshold})`;
  }
  return label;
}

interface RuleFormState {
  name: string;
  metric: string;
  instance: string;
  op: string;
  threshold: string;
  clear_threshold: string;
  sustain_seconds: string;
  severity: string;
  enabled: boolean;
}

function formFromRule(rule: AlertRule | null, metrics: AlertMetricInfo[]): RuleFormState {
  if (rule === null) {
    return {
      name: "",
      metric: metrics[0]?.metric ?? "",
      instance: "*",
      op: ">",
      threshold: "",
      clear_threshold: "",
      sustain_seconds: "60",
      severity: "warning",
      enabled: true,
    };
  }
  return {
    name: rule.name,
    metric: rule.metric,
    instance: rule.instance,
    op: rule.op,
    threshold: String(rule.threshold),
    clear_threshold: rule.clear_threshold === null ? "" : String(rule.clear_threshold),
    sustain_seconds: String(rule.sustain_seconds),
    severity: rule.severity,
    enabled: rule.enabled,
  };
}

interface RuleEditorProps {
  rule: AlertRule | null; // null = create
  metrics: AlertMetricInfo[];
  onSaved: () => void;
  onCancel: () => void;
}

function RuleEditor({ rule, metrics, onSaved, onCancel }: RuleEditorProps) {
  const [form, setForm] = useState<RuleFormState>(() => formFromRule(rule, metrics));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const metricInfo = metrics.find((m) => m.metric === form.metric);
  const globalMetric = metricInfo?.scope === "global";
  // Cost metrics key their rules on spender names rather than coordinators;
  // the field label follows the metric's scope so '*' reads correctly.
  const keyLabel =
    metricInfo?.scope === "commander"
      ? "Commander"
      : metricInfo?.scope === "device"
        ? "Device"
        : "Instance";

  function set<K extends keyof RuleFormState>(key: K, value: RuleFormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body = {
      name: form.name,
      metric: form.metric,
      instance: globalMetric ? "*" : form.instance || "*",
      op: form.op,
      threshold: Number(form.threshold),
      clear_threshold: form.clear_threshold === "" ? null : Number(form.clear_threshold),
      sustain_seconds: Number(form.sustain_seconds),
      severity: form.severity,
      enabled: form.enabled,
    };
    try {
      if (rule === null) {
        await api("/api/alerts/rules", { method: "POST", body: JSON.stringify(body) });
      } else {
        await api(`/api/alerts/rules/${rule.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to reach the collector");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={(event) => void handleSubmit(event)}>
      <div className="row">
        <label className="grow">
          Rule name
          <input
            value={form.name}
            onChange={(event) => set("name", event.target.value)}
            required
          />
        </label>
        <label>
          Severity
          <select
            value={form.severity}
            onChange={(event) => set("severity", event.target.value)}
          >
            {SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>
                {severity}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="row">
        <label className="grow">
          Metric
          <select value={form.metric} onChange={(event) => set("metric", event.target.value)}>
            {metrics.map((metric) => (
              <option key={metric.metric} value={metric.metric}>
                {metric.metric} ({metric.unit})
              </option>
            ))}
          </select>
        </label>
        <label>
          {keyLabel}
          <input
            value={globalMetric ? "*" : form.instance}
            onChange={(event) => set("instance", event.target.value)}
            disabled={globalMetric}
            placeholder="* (all)"
          />
        </label>
      </div>
      {metricInfo && <p className="hint">{metricInfo.description}</p>}
      <div className="row">
        <label>
          Condition
          <select value={form.op} onChange={(event) => set("op", event.target.value)}>
            <option value=">">above (&gt;)</option>
            <option value="<">below (&lt;)</option>
          </select>
        </label>
        <label>
          Threshold
          <input
            type="number"
            step="any"
            value={form.threshold}
            onChange={(event) => set("threshold", event.target.value)}
            required
          />
        </label>
        <label>
          Clear threshold
          <input
            type="number"
            step="any"
            value={form.clear_threshold}
            onChange={(event) => set("clear_threshold", event.target.value)}
            placeholder="= threshold"
          />
        </label>
        <label>
          Sustain (s)
          <input
            type="number"
            min="0"
            max="86400"
            value={form.sustain_seconds}
            onChange={(event) => set("sustain_seconds", event.target.value)}
            required
          />
        </label>
      </div>
      <p className="hint">
        Opens after the condition holds for the sustain window; clears after the value stays
        on the OK side of the clear threshold for max(sustain, 60 s).
      </p>
      <label className="check-row">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(event) => set("enabled", event.target.checked)}
        />
        <span>Enabled</span>
      </label>
      {error && <p className="error">{error}</p>}
      <div className="row">
        <button type="submit" disabled={busy || !form.name || form.threshold === ""}>
          {busy ? "…" : rule === null ? "Create rule" : "Save rule"}
        </button>
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function ActivePanel({ active }: { active: ActiveAlert[] }) {
  return (
    <div className="panel">
      <p className="panel-kicker">Active alerts</p>
      {active.length === 0 ? (
        <p className="hint">No active alerts.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Severity</th>
              <th>Alert</th>
              <th>Instance</th>
              <th className="num">Value</th>
              <th className="num">Peak</th>
              <th className="num">Threshold</th>
              <th className="num">Open for</th>
            </tr>
          </thead>
          <tbody>
            {active.map((alert) => (
              <tr key={alert.event_id}>
                <td>
                  <span className={severityChip(alert.severity)}>
                    {alert.severity ?? "?"}
                  </span>
                </td>
                <td>{alert.name ?? `rule #${alert.rule_id}`}</td>
                <td className="mono">{alert.instance}</td>
                <td className="num">{formatValue(alert.value, alert.unit)}</td>
                <td className="num">{formatValue(alert.peak, alert.unit)}</td>
                <td className="num">
                  {alert.op} {formatValue(alert.threshold, alert.unit)}
                </td>
                <td className="num" title={formatTime(alert.opened_at)}>
                  {formatSince(alert.opened_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function Alerts() {
  const [view, setView] = useState<AlertsView | null>(null);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [historySeconds, setHistorySeconds] = useState(86400);
  const [editing, setEditing] = useState<AlertRule | "new" | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setView(await api<AlertsView>("/api/alerts"));
      const history = await api<{ events: AlertEvent[] }>(
        `/api/alerts/history?seconds=${historySeconds}`,
      );
      setEvents(history.events);
    } catch {
      setError("Failed to load alerts");
    }
  }, [historySeconds]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function toggleRule(rule: AlertRule) {
    setBusy(rule.id);
    setError(null);
    try {
      await api(`/api/alerts/rules/${rule.id}`, {
        method: "PUT",
        body: JSON.stringify({ ...rule, enabled: !rule.enabled }),
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed");
    } finally {
      setBusy(null);
      void refresh();
    }
  }

  async function deleteRule(rule: AlertRule) {
    setBusy(rule.id);
    setError(null);
    try {
      await api(`/api/alerts/rules/${rule.id}`, { method: "DELETE" });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed");
    } finally {
      setBusy(null);
      void refresh();
    }
  }

  if (view === null) {
    return <p className="hint">loading…</p>;
  }

  return (
    <>
      <ActivePanel active={view.active} />
      <div className="panel">
        <p className="panel-kicker">Rules</p>
        <p className="hint">
          Self-health rules ship enabled: they only fire when something you deployed stops
          reporting. Capacity rules ship disabled with placeholder thresholds: enable the
          ones you want once you know your installation's norms. A rule with instance{" "}
          <code>*</code> watches every instance reporting the metric.
        </p>
        {error && <p className="error">{error}</p>}
        <table className="table">
          <thead>
            <tr>
              <th></th>
              <th>Rule</th>
              <th>Condition</th>
              <th>Instance</th>
              <th className="num">Sustain</th>
              <th>Severity</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {view.rules.map((rule) => (
              <tr key={rule.id} className={rule.enabled ? "" : "dim"}>
                <td>
                  <input
                    type="checkbox"
                    checked={rule.enabled}
                    disabled={busy !== null}
                    onChange={() => void toggleRule(rule)}
                    title={rule.enabled ? "Disable rule" : "Enable rule"}
                  />
                </td>
                <td>
                  {rule.name}
                  {rule.builtin && <span className="hint"> · built-in</span>}
                </td>
                <td className="mono">{conditionLabel(rule)}</td>
                <td className="mono">{rule.instance}</td>
                <td className="num">{rule.sustain_seconds}s</td>
                <td>
                  <span className={severityChip(rule.severity)}>{rule.severity}</span>
                </td>
                <td className="num">
                  <button
                    className="ghost small"
                    disabled={busy !== null}
                    onClick={() => setEditing(rule)}
                  >
                    Edit
                  </button>{" "}
                  <button
                    className="ghost small"
                    disabled={busy !== null}
                    onClick={() => void deleteRule(rule)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {editing === null ? (
          <button className="small" onClick={() => setEditing("new")}>
            New rule
          </button>
        ) : (
          <RuleEditor
            rule={editing === "new" ? null : editing}
            metrics={view.metrics}
            onSaved={() => {
              setEditing(null);
              void refresh();
            }}
            onCancel={() => setEditing(null)}
          />
        )}
      </div>
      <div className="panel">
        <p className="panel-kicker">
          Event history{" "}
          <select
            value={historySeconds}
            onChange={(event) => setHistorySeconds(Number(event.target.value))}
          >
            {HISTORY_WINDOWS.map((window) => (
              <option key={window.seconds} value={window.seconds}>
                {window.label}
              </option>
            ))}
          </select>
        </p>
        {events.length === 0 ? (
          <p className="hint">No events in this window. History keeps 90 days.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Severity</th>
                <th>Alert</th>
                <th>Instance</th>
                <th>Opened</th>
                <th>Cleared</th>
                <th className="num">Peak</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>
                    <span className={severityChip(event.context.severity)}>
                      {event.context.severity ?? "?"}
                    </span>
                  </td>
                  <td>
                    {event.context.name ?? `rule #${event.rule_id}`}
                    {event.context.closed && (
                      <span className="hint"> · {event.context.closed}</span>
                    )}
                  </td>
                  <td className="mono">{event.instance}</td>
                  <td>{formatTime(event.opened_at)}</td>
                  <td>{event.cleared_at ? formatTime(event.cleared_at) : "still open"}</td>
                  <td className="num">{formatValue(event.peak_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
