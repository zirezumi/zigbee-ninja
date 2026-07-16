import { useCallback, useEffect, useState } from "react";
import { api, AttributionSummary, LedgerView, RedundantRow } from "../api";

const WINDOWS: Array<[label: string, seconds: number]> = [
  ["15 min", 900],
  ["1 h", 3600],
  ["24 h", 86400],
];

const CLASS_ORDER = [
  "commanded",
  "provoked",
  "autonomous",
  "controller-housekeeping",
  "stack-housekeeping",
  "retry-overhead",
  "self",
];

const CLASS_TITLES: Record<string, string> = {
  commanded:
    "Commands sent because something outside Zigbee2MQTT asked: automations, scripts, or users publishing to …/set and …/get",
  provoked:
    "Replies those commands caused: read responses and the state echoes that follow a set",
  autonomous:
    "Traffic devices generate on their own: sensor reports, physical presses; outside any command's window",
  "controller-housekeeping":
    "Zigbee2MQTT's own radio work: availability pings, configuration reads, firmware-update checks",
  "stack-housekeeping":
    "The coordinator chip's internal network upkeep: modeled, since it never crosses an observable boundary",
  "retry-overhead": "Extra transmissions spent repeating frames that failed the first time",
  self: "zigbee-ninja's own traffic (topology pulls, calibration reads); always counted separately",
};

/** Percent of the channel's usable airtime; the ledger's headline unit. */
export function fmtBudgetPct(pct: number): string {
  if (pct === 0) return "~0%";
  if (pct < 0.01) return "<0.01%";
  return `${pct.toFixed(2)}%`;
}

export function fmtUsPerS(value: number): string {
  if (value >= 100) return `${Math.round(value)} µs/s`;
  if (value >= 1) return `${value.toFixed(1)} µs/s`;
  return `${value.toFixed(2)} µs/s`;
}

const COST_TITLE =
  "Modeled radio airtime this spender's traffic cost, from the cost ledger: " +
  "group commands are multiplied across every router that relays them, " +
  "unicasts by the measured retry rate. Ledger entries accumulate per UTC " +
  "day, so these columns cover the listed days, not the window above.";

const BUDGET_TITLE =
  "Share of one channel's usable airtime budget (250 kbps less protocol " +
  "overhead) this spender's average rate would occupy";

function ClassBar({ classes }: { classes: Record<string, number> }) {
  const total = Object.values(classes).reduce((sum, count) => sum + count, 0);
  if (total === 0) return <div className="classbar empty" />;
  return (
    <div className="classbar">
      {CLASS_ORDER.filter((klass) => classes[klass]).map((klass) => (
        <span
          key={klass}
          className={`classbar-seg seg-${klass}`}
          style={{ width: `${(classes[klass] / total) * 100}%` }}
          title={`${klass}: ${classes[klass]}; ${CLASS_TITLES[klass] ?? ""}`}
        />
      ))}
    </div>
  );
}

export default function Attribution() {
  const [seconds, setSeconds] = useState(3600);
  const [summary, setSummary] = useState<AttributionSummary | null>(null);
  const [redundant, setRedundant] = useState<RedundantRow[]>([]);
  const [ledger, setLedger] = useState<LedgerView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [summaryData, redundantData, ledgerData] = await Promise.all([
        api<AttributionSummary>(`/api/attribution/summary?seconds=${seconds}`),
        api<{ redundant: RedundantRow[] }>(`/api/attribution/redundant?seconds=${seconds}`),
        api<LedgerView>(`/api/ledger?seconds=${seconds}`),
      ]);
      setSummary(summaryData);
      setRedundant(redundantData.redundant);
      setLedger(ledgerData);
    } catch {
      setError("Failed to load attribution data");
    }
  }, [seconds]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 30000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const instances = Object.keys(summary?.classes ?? {}).sort();

  const commanderCosts = new Map<string, { us_per_s: number; pct_of_budget: number }>();
  for (const row of ledger?.commanders ?? []) {
    const entry = commanderCosts.get(row.commander) ?? { us_per_s: 0, pct_of_budget: 0 };
    entry.us_per_s += row.us_per_s;
    entry.pct_of_budget += row.pct_of_budget;
    commanderCosts.set(row.commander, entry);
  }

  const spenders = [
    ...(ledger?.commanders ?? []).map((row) => ({
      key: `c/${row.instance}/${row.commander}`,
      name: row.commander,
      kind: "commands",
      instance: row.instance,
      us_per_s: row.us_per_s,
      pct_of_budget: row.pct_of_budget,
      traffic: `${row.chains} ${row.chains === 1 ? "chain" : "chains"}`,
      title:
        `Priced with ${row.params.n_routers} routers, ` +
        `broadcast relay factor ${row.params.avg_tx}` +
        `${row.params.avg_tx_measured ? " (measured)" : " (modeled)"}, ` +
        `retry rate ${row.params.retry_rate}` +
        `${row.params.retry_rate_measured ? " (measured)" : " (default)"}; ${row.provenance}`,
    })),
    ...(ledger?.devices ?? []).map((row) => ({
      key: `d/${row.instance}/${row.device}`,
      name: row.device,
      kind: "reporting",
      instance: row.instance,
      us_per_s: row.us_per_s,
      pct_of_budget: row.pct_of_budget,
      traffic: `${row.publishes} ${row.publishes === 1 ? "report" : "reports"}`,
      title: row.provenance,
    })),
  ]
    .sort((a, b) => b.us_per_s - a.us_per_s)
    .slice(0, 12);

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
        {summary && (
          <span className="hint">
            {summary.totals.chains} command chains · {summary.totals.redundant} redundant
            {summary.totals.avg_first_echo_ms != null
              ? ` · ${Math.round(summary.totals.avg_first_echo_ms)} ms avg first echo`
              : ""}
          </span>
        )}
        <button className="ghost small" onClick={() => void refresh()}>
          Refresh
        </button>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="panel">
        <p className="panel-kicker">Traffic by causality class</p>
        {instances.length === 0 ? (
          <p className="hint">
            No classified traffic in this window yet. Classes appear once commands and
            state publishes flow through the collector.
          </p>
        ) : (
          <div className="classrows">
            {instances.map((instance) => (
              <div key={instance} className="classrow">
                <span className="mono">{instance}</span>
                <ClassBar classes={summary!.classes[instance]} />
                <span className="classrow-detail">
                  {CLASS_ORDER.filter((klass) => summary!.classes[instance][klass]).map(
                    (klass) => (
                      <span
                        key={klass}
                        className={`kind legend-${klass}`}
                        title={CLASS_TITLES[klass]}
                      >
                        <span className="kind-name">{klass}</span>
                        <span className="kind-count">{summary!.classes[instance][klass]}</span>
                      </span>
                    ),
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <p className="panel-kicker" title={COST_TITLE}>
          Top spenders
        </p>
        {spenders.length === 0 ? (
          <p className="hint">
            No priced traffic yet. Every command chain and device report is priced in
            modeled radio airtime and accumulated here per UTC day.
          </p>
        ) : (
          <>
            <table className="table">
              <thead>
                <tr>
                  <th>Spender</th>
                  <th>Kind</th>
                  <th>Instance</th>
                  <th className="num" title={BUDGET_TITLE}>
                    % of budget
                  </th>
                  <th className="num" title="Average airtime spend across the covered days">
                    Airtime
                  </th>
                  <th className="num">Traffic</th>
                </tr>
              </thead>
              <tbody>
                {spenders.map((row) => (
                  <tr key={row.key} title={row.title}>
                    <td className="mono">{row.name}</td>
                    <td>{row.kind}</td>
                    <td className="mono">{row.instance}</td>
                    <td className="num">{fmtBudgetPct(row.pct_of_budget)}</td>
                    <td className="num">{fmtUsPerS(row.us_per_s)}</td>
                    <td className="num">{row.traffic}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {ledger && (
              <p className="hint">
                Everything above sums to {fmtBudgetPct(ledger.totals.pct_of_budget)} of one
                channel's budget ({fmtUsPerS(ledger.totals.us_per_s)}) over{" "}
                {ledger.days.length === 1
                  ? `the UTC day ${ledger.days[0]}`
                  : `UTC days ${ledger.days[0]} to ${ledger.days[ledger.days.length - 1]}`}
                . Costs are modeled from message shapes and the router census; measured
                retry and relay factors fold in as the wiretap gathers them. Comparable
                estimates, not meter readings.
              </p>
            )}
          </>
        )}
      </div>

      <div className="panel-grid">
        <div className="panel">
          <p className="panel-kicker">Top commanded targets</p>
          <table className="table">
            <thead>
              <tr>
                <th>Instance</th>
                <th>Target</th>
                <th className="num">Cmds</th>
                <th className="num">Redundant</th>
                <th className="num">First echo</th>
              </tr>
            </thead>
            <tbody>
              {(summary?.top_targets ?? []).map((row) => (
                <tr key={`${row.instance}/${row.target}`}>
                  <td className="mono">{row.instance}</td>
                  <td>{row.target}</td>
                  <td className="num">{row.commands}</td>
                  <td className="num">{row.redundant || ""}</td>
                  <td className="num">
                    {row.avg_first_echo_ms != null ? `${Math.round(row.avg_first_echo_ms)} ms` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="panel">
          <p className="panel-kicker">Commanders (MQTT clients)</p>
          <table className="table">
            <thead>
              <tr>
                <th>Client</th>
                <th className="num">Commands</th>
                <th className="num" title={BUDGET_TITLE}>
                  % of budget
                </th>
                <th className="num" title={COST_TITLE}>
                  Airtime
                </th>
              </tr>
            </thead>
            <tbody>
              {(summary?.top_clients ?? []).map((row) => {
                const cost = commanderCosts.get(row.client);
                return (
                  <tr key={row.client}>
                    <td className="mono">
                      {row.client}
                      {row.label && <span className="hint"> · {row.label}</span>}
                    </td>
                    <td className="num">{row.commands}</td>
                    <td className="num">
                      {cost ? fmtBudgetPct(cost.pct_of_budget) : "—"}
                    </td>
                    <td className="num">{cost ? fmtUsPerS(cost.us_per_s) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="hint">
            Commander identity comes from the Home Assistant integration (Permissions
            page): a read-only HA connection names the automation or script behind each{" "}
            <code>mqtt.publish</code>. Without it, commands show as (unattributed).
          </p>
        </div>

        <div className="panel">
          <p className="panel-kicker">Redundant commands</p>
          {redundant.length === 0 ? (
            <p className="hint">
              None detected: identical payloads to the same target within 5 s would appear
              here. The cheapest utilization win there is.
            </p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Instance</th>
                  <th>Target</th>
                  <th>Client</th>
                  <th className="num">Count</th>
                </tr>
              </thead>
              <tbody>
                {redundant.map((row) => (
                  <tr key={`${row.instance}/${row.target}/${row.client}`}>
                    <td className="mono">{row.instance}</td>
                    <td>{row.target}</td>
                    <td className="mono">{row.client}</td>
                    <td className="num">{row.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}
