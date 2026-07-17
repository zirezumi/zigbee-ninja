import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  AdvisorScore,
  SavedScenario,
  ScenarioContext,
  ScenarioContextDevice,
  ScenarioContextGroup,
  ScenarioContextInstance,
  ScenarioMove,
  ScenarioPriceReport,
  ScenarioScoreReport,
} from "../api";

const PRICE_DEBOUNCE_MS = 400;

type SortKey = "spend" | "name";
type KindFilter = "all" | "routers" | "end_devices";

function usPerS(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (Math.abs(value) >= 100) return `${Math.round(value)} µs/s`;
  return `${value.toFixed(1)} µs/s`;
}

function eps(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 10) / 10}/s`;
}

function delta(before: number, after: number): string {
  const diff = after - before;
  if (Math.abs(diff) < 0.05) return "±0";
  return `${diff > 0 ? "+" : "−"}${usPerS(Math.abs(diff)).replace(" µs/s", "")}`;
}

/** Verdict chip: the §V2-11 warning states. Near the sustained limit warns;
 * crossing the sustained limit or the ceiling is the bad state. */
function VerdictChip({ verdict }: { verdict: string }) {
  const labels: Record<string, [string, string, string]> = {
    ok: ["within limits", "chip ok", "The peak sits comfortably under the measured sustained capacity limit"],
    near_sustained: [
      "near the limit",
      "chip warn",
      "The peak reaches 80% of the measured sustained capacity limit; bursts this size queue behind each other under load",
    ],
    above_sustained: [
      "above sustained",
      "chip bad",
      "The peak exceeds what this coordinator sustains; commands queue briefly during such bursts",
    ],
    above_ceiling: [
      "above ceiling",
      "chip bad",
      "The peak exceeds the highest rate the benchmark ever achieved; bursts this size stall until the queue drains",
    ],
    no_limits: [
      "no measured limit",
      "chip",
      "No capacity benchmark has run for this coordinator, so peaks cannot be judged",
    ],
    no_traffic: ["no traffic", "chip", "No commands were recorded in this window"],
  };
  const [label, className, tip] = labels[verdict] ?? [verdict, "chip", ""];
  return (
    <span className={className} title={tip}>
      {label}
    </span>
  );
}

function moveKey(move: ScenarioMove): string {
  return `${move.kind}:${move.from_instance}:${move.subject}`;
}

interface ChipProps {
  label: string;
  cost: number;
  router?: boolean;
  stagedTo?: string | null;
  viaGroup?: string | null;
  lanes: string[];
  home: string;
  onMove: (to: string) => void;
  onDragStart: () => void;
  title?: string;
}

function SubjectChip({
  label,
  cost,
  router,
  stagedTo,
  viaGroup,
  lanes,
  home,
  onMove,
  onDragStart,
  title,
}: ChipProps) {
  return (
    <div
      className={stagedTo || viaGroup ? "subject-chip staged" : "subject-chip"}
      draggable={!viaGroup}
      onDragStart={(event) => {
        event.dataTransfer.setData("text/plain", label);
        event.dataTransfer.effectAllowed = "move";
        onDragStart();
      }}
      title={title}
    >
      <span className="subject-name">
        {router && (
          <span
            className="router-glyph"
            title="Router: it relays broadcasts, so moving it changes the amplification cost of every group command on both meshes"
          >
            R
          </span>
        )}
        {label}
      </span>
      <span className="subject-cost" title="Recorded airtime this subject costs per second (commands to it plus its own reports), in the same modeled currency as Top spenders">
        {usPerS(cost)}
      </span>
      {stagedTo ? (
        <span className="chip warn" title="This subject is staged to move">
          → {stagedTo}
        </span>
      ) : viaGroup ? (
        <span className="chip" title={`Travels with the staged move of ${viaGroup}`}>
          with {viaGroup}
        </span>
      ) : (
        <select
          className="move-select"
          value=""
          title="Stage a move without dragging"
          onChange={(event) => {
            if (event.target.value) onMove(event.target.value);
          }}
        >
          <option value="">Move to…</option>
          {lanes
            .filter((lane) => lane !== home)
            .map((lane) => (
              <option key={lane} value={lane}>
                {lane}
              </option>
            ))}
        </select>
      )}
    </div>
  );
}

export default function Rebalance() {
  const [context, setContext] = useState<ScenarioContext | null>(null);
  const [moves, setMoves] = useState<ScenarioMove[]>([]);
  const [report, setReport] = useState<ScenarioPriceReport | null>(null);
  const [score, setScore] = useState<AdvisorScore | null>(null);
  const [saved, setSaved] = useState<SavedScenario[]>([]);
  const [saveName, setSaveName] = useState("");
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("spend");
  const [pricing, setPricing] = useState(false);
  const [scoring, setScoring] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [replayPreview, setReplayPreview] = useState<Record<string, unknown> | null>(null);
  const [replayInstance, setReplayInstance] = useState<string | null>(null);
  const [replayBusy, setReplayBusy] = useState(false);
  const [replayStarted, setReplayStarted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dragging = useRef<ScenarioMove | null>(null);

  const loadContext = useCallback(async () => {
    try {
      setContext(await api<ScenarioContext>("/api/scenario/context"));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load the fleet");
    }
  }, []);

  const loadSaved = useCallback(async () => {
    try {
      const body = await api<{ scenarios: SavedScenario[] }>("/api/scenario/saved");
      setSaved(body.scenarios);
    } catch {
      // saved scenarios are a convenience; the lanes stand alone
    }
  }, []);

  useEffect(() => {
    void loadContext();
    void loadSaved();
  }, [loadContext, loadSaved]);

  // Staged moves reprice through the backend engine after a short pause;
  // the GUI never does its own cost math.
  const movesKey = JSON.stringify(moves);
  useEffect(() => {
    setScore(null);
    if (moves.length === 0) {
      setReport(null);
      setPricing(false);
      return;
    }
    setPricing(true);
    const timer = window.setTimeout(async () => {
      try {
        const priced = await api<ScenarioPriceReport>("/api/scenario/price", {
          method: "POST",
          body: JSON.stringify({ moves }),
        });
        setReport(priced);
        setError(null);
      } catch (err) {
        setReport(null);
        setError(
          err instanceof ApiError ? err.message : "Failed to price the scenario",
        );
      } finally {
        setPricing(false);
      }
    }, PRICE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [movesKey]);

  const lanes = useMemo(
    () => (context ? Object.keys(context.instances).sort() : []),
    [context],
  );

  const stagedByKey = useMemo(() => {
    const map = new Map<string, ScenarioMove>();
    for (const move of moves) map.set(moveKey(move), move);
    return map;
  }, [moves]);

  /** Devices travelling because their group is staged, keyed per lane. */
  const viaGroupByLane = useMemo(() => {
    const map = new Map<string, Map<string, string>>();
    if (!context) return map;
    for (const move of moves) {
      if (move.kind !== "group") continue;
      const instance = context.instances[move.from_instance];
      const group = instance?.groups.find((g) => g.name === move.subject);
      if (!group) continue;
      const laneMap = map.get(move.from_instance) ?? new Map<string, string>();
      for (const member of group.members) laneMap.set(member, move.subject);
      map.set(move.from_instance, laneMap);
    }
    return map;
  }, [moves, context]);

  function stageMove(kind: "device" | "group", subject: string, from: string, to: string) {
    if (from === to) return;
    setMoves((current) => {
      const withoutSubject = current.filter(
        (move) => !(move.kind === kind && move.subject === subject && move.from_instance === from),
      );
      return [...withoutSubject, { kind, subject, from_instance: from, to_instance: to }];
    });
  }

  function removeMove(index: number) {
    setMoves((current) => current.filter((_, i) => i !== index));
  }

  function setResolution(split: { movers: string[]; instance: string }, resolution: "unicasts" | "new_group") {
    setMoves((current) =>
      current.map((move) =>
        move.kind === "device" &&
        move.from_instance === split.instance &&
        split.movers.includes(move.subject)
          ? { ...move, group_resolution: resolution }
          : move,
      ),
    );
  }

  async function scoreNow() {
    setScoring(true);
    try {
      const scored = await api<ScenarioScoreReport>("/api/scenario/score", {
        method: "POST",
        body: JSON.stringify({ moves }),
      });
      setReport(scored);
      setScore(scored.advisor);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Scoring failed");
    } finally {
      setScoring(false);
    }
  }

  async function exportManifest() {
    setExporting(true);
    try {
      const manifest = await api<Record<string, unknown>>("/api/scenario/manifest", {
        method: "POST",
        body: JSON.stringify({ moves, source: "simulator" }),
      });
      const blob = new Blob([JSON.stringify(manifest, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `migration-manifest-${new Date().toISOString().slice(0, 10)}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Manifest export failed");
    } finally {
      setExporting(false);
    }
  }

  async function previewReplay(dest: string) {
    setReplayBusy(true);
    setReplayStarted(false);
    try {
      const plan = await api<Record<string, unknown>>(
        "/api/calibration/replay/preview",
        {
          method: "POST",
          body: JSON.stringify({
            instance: dest,
            source: { kind: "scenario", moves, variant: "as_recorded" },
          }),
        },
      );
      setReplayPreview(plan);
      setReplayInstance(dest);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Replay preview failed");
    } finally {
      setReplayBusy(false);
    }
  }

  async function authorizeReplay() {
    if (!replayPreview || !replayInstance) return;
    setReplayBusy(true);
    try {
      await api("/api/calibration/replay/run", {
        method: "POST",
        body: JSON.stringify({
          instance: replayInstance,
          authorization: replayPreview.authorization,
        }),
      });
      setReplayPreview(null);
      setReplayStarted(true);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Replay refused");
    } finally {
      setReplayBusy(false);
    }
  }

  async function saveScenario() {
    const name = saveName.trim();
    if (!name || moves.length === 0) return;
    try {
      await api("/api/scenario/saved", {
        method: "POST",
        body: JSON.stringify({ name, moves }),
      });
      setSaveName("");
      await loadSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Save failed");
    }
  }

  async function deleteScenario(name: string) {
    try {
      await api(`/api/scenario/saved/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      await loadSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed");
    }
  }

  function matchesSearch(name: string): boolean {
    return name.toLowerCase().includes(search.trim().toLowerCase());
  }

  function visibleDevices(instance: ScenarioContextInstance): ScenarioContextDevice[] {
    let devices = instance.devices;
    if (kindFilter === "routers") devices = devices.filter((d) => d.router);
    if (kindFilter === "end_devices") devices = devices.filter((d) => !d.router);
    if (search.trim()) devices = devices.filter((d) => matchesSearch(d.name));
    return [...devices].sort((a, b) =>
      sortKey === "spend" ? b.us_per_s - a.us_per_s : a.name.localeCompare(b.name),
    );
  }

  function visibleGroups(instance: ScenarioContextInstance): ScenarioContextGroup[] {
    let groups = instance.groups;
    if (search.trim()) {
      groups = groups.filter(
        (g) => matchesSearch(g.name) || g.members.some((m) => matchesSearch(m)),
      );
    }
    return [...groups].sort((a, b) =>
      sortKey === "spend" ? b.us_per_s - a.us_per_s : a.name.localeCompare(b.name),
    );
  }

  function laneHeader(base: string, instance: ScenarioContextInstance) {
    const after = report?.instances[base];
    const steadyBefore = instance.steady.us_per_s;
    const steadyAfter = after?.steady.after_us_per_s;
    const peakBefore = instance.burst.peak_1s?.eps_1s ?? null;
    const peakAfter = after ? after.burst.after_peak_1s?.eps_1s ?? null : null;
    const verdict = after ? after.burst.verdict : instance.burst.verdict;
    const limit = instance.limits?.sustained_eps;
    const ceiling = instance.limits?.ceiling_eps;
    return (
      <div className="lane-head">
        <div className="lane-title">
          <strong>{base}</strong>
          {instance.channel !== null && (
            <span className="chip" title="Zigbee radio channel; coordinators sharing a channel draw one pooled airtime budget">
              ch {instance.channel}
            </span>
          )}
          <span
            className="chip"
            title="Mains-powered routers on this mesh; each relays every group command, so the census sets broadcast amplification"
          >
            {after ? after.census.routers_after : instance.census.routers} routers
          </span>
        </div>
        <div className="lane-fact" title="Modeled steady airtime of this coordinator's recorded traffic (commands plus reports), and its share of the shared radio channel's budget">
          <span>steady</span>
          <span>
            {usPerS(steadyBefore)}
            {steadyAfter !== undefined && steadyAfter !== null && (
              <>
                {" "}
                → <strong>{usPerS(steadyAfter)}</strong>{" "}
                <span className="chip">{delta(steadyBefore, steadyAfter)}</span>
              </>
            )}
          </span>
        </div>
        <div className="lane-fact" title="Highest recorded 1 s command rate in the window, judged against the benchmark-measured sustained limit and ceiling; the staged number is a modeled recomposition of the same recorded commands">
          <span>burst peak</span>
          <span>
            {eps(peakBefore)}
            {report && (
              <>
                {" "}
                → <strong>{eps(peakAfter)}</strong>
              </>
            )}
            {limit ? ` of ${eps(limit)}` : ""}
            {ceiling ? ` (ceiling ${eps(ceiling)})` : ""}
          </span>
        </div>
        <VerdictChip verdict={verdict} />
        {instance.limits?.stale_environment && (
          <span
            className="chip warn"
            title="The capacity limit was measured under a different Zigbee2MQTT or firmware version; consider recalibrating"
          >
            limit stale
          </span>
        )}
      </div>
    );
  }

  function laneBody(base: string, instance: ScenarioContextInstance) {
    const viaGroup = viaGroupByLane.get(base);
    const arriving = moves.filter((move) => move.to_instance === base);
    const groups = visibleGroups(instance);
    const grouped = new Set(groups.flatMap((g) => g.members));
    const devices = visibleDevices(instance);
    return (
      <div
        className="lane-body"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          const staged = dragging.current;
          dragging.current = null;
          if (staged) stageMove(staged.kind, staged.subject, staged.from_instance, base);
        }}
      >
        {arriving.length > 0 && (
          <div className="arriving" title="Subjects staged to move onto this coordinator">
            {arriving.map((move) => (
              <span key={moveKey(move)} className="chip warn">
                ← {move.subject}
              </span>
            ))}
          </div>
        )}
        {groups.map((group) => (
          <div key={group.name} className="group-box">
            <SubjectChip
              label={group.name}
              cost={group.us_per_s}
              stagedTo={stagedByKey.get(`group:${base}:${group.name}`)?.to_instance ?? null}
              lanes={lanes}
              home={base}
              onMove={(to) => stageMove("group", group.name, base, to)}
              onDragStart={() => {
                dragging.current = {
                  kind: "group",
                  subject: group.name,
                  from_instance: base,
                  to_instance: base,
                };
              }}
              title="A Zigbee group: its member devices travel with it. Dragging a member out on its own breaks the group; both repair options get priced."
            />
            <div className="group-members">
              {group.members
                .filter((member) => !search.trim() || matchesSearch(member) || matchesSearch(group.name))
                .map((member) => {
                  const device = instance.devices.find((d) => d.name === member);
                  if (!device) return null;
                  if (kindFilter === "routers" && !device.router) return null;
                  if (kindFilter === "end_devices" && device.router) return null;
                  return (
                    <SubjectChip
                      key={member}
                      label={member}
                      cost={device.us_per_s}
                      router={device.router}
                      stagedTo={
                        stagedByKey.get(`device:${base}:${member}`)?.to_instance ?? null
                      }
                      viaGroup={viaGroup?.get(member) ?? null}
                      lanes={lanes}
                      home={base}
                      onMove={(to) => stageMove("device", member, base, to)}
                      onDragStart={() => {
                        dragging.current = {
                          kind: "device",
                          subject: member,
                          from_instance: base,
                          to_instance: base,
                        };
                      }}
                    />
                  );
                })}
            </div>
          </div>
        ))}
        {devices
          .filter((device) => !grouped.has(device.name))
          .map((device) => (
            <SubjectChip
              key={device.name}
              label={device.name}
              cost={device.us_per_s}
              router={device.router}
              stagedTo={stagedByKey.get(`device:${base}:${device.name}`)?.to_instance ?? null}
              viaGroup={viaGroup?.get(device.name) ?? null}
              lanes={lanes}
              home={base}
              onMove={(to) => stageMove("device", device.name, base, to)}
              onDragStart={() => {
                dragging.current = {
                  kind: "device",
                  subject: device.name,
                  from_instance: base,
                  to_instance: base,
                };
              }}
            />
          ))}
      </div>
    );
  }

  function trayLine(move: ScenarioMove, index: number) {
    const reported = report?.moves.find(
      (entry) =>
        entry.kind === move.kind &&
        entry.subject === move.subject &&
        entry.from_instance === move.from_instance,
    );
    const splits = (report?.splits ?? []).filter(
      (split) =>
        split.instance === move.from_instance && split.movers.includes(move.subject),
    );
    return (
      <div key={moveKey(move)} className="tray-move">
        <div className="row">
          <span className="grow">
            <strong>{move.subject}</strong>
            {move.kind === "group" ? " (group)" : ""} · {move.from_instance} →{" "}
            {move.to_instance}
          </span>
          {reported && (
            <span
              className="chip"
              title="Modeled steady airtime of this subject's recorded traffic, priced on the source and destination meshes"
            >
              {usPerS(
                reported.commands.before_us_per_s + (reported.reports?.us_per_s ?? 0),
              )}{" "}
              →{" "}
              {usPerS(
                reported.commands.after_us_per_s + (reported.reports?.us_per_s ?? 0),
              )}
            </span>
          )}
          {reported?.radio && (
            <span
              className="chip"
              title="Whether this device can reach the destination coordinator by radio cannot be known from recorded data. Best observed link quality and the destination channel are context, not a prediction."
            >
              radio unknown
              {reported.radio.best_observed_link_lqi !== null
                ? ` · LQI ${reported.radio.best_observed_link_lqi}`
                : ""}
            </span>
          )}
          <button className="ghost small" onClick={() => removeMove(index)}>
            Remove
          </button>
        </div>
        {splits.map((split) => (
          <div key={split.group} className="hint split-note">
            Moving {split.movers.join(", ")} breaks the group {split.group} (
            {split.stayers} member{split.stayers === 1 ? "" : "s"} stay behind).
            Repair:{" "}
            <label className="inline-radio">
              <input
                type="radio"
                name={`res-${split.group}-${index}`}
                checked={split.applied_resolution === "unicasts"}
                onChange={() => setResolution(split, "unicasts")}
              />
              per-device commands ({usPerS(split.added_us_per_s.unicasts)})
            </label>
            <label className="inline-radio">
              <input
                type="radio"
                name={`res-${split.group}-${index}`}
                checked={split.applied_resolution === "new_group"}
                onChange={() => setResolution(split, "new_group")}
              />
              new group on {split.to_instance} ({usPerS(split.added_us_per_s.new_group)})
            </label>
          </div>
        ))}
      </div>
    );
  }

  if (context === null) {
    return error ? <p className="error">{error}</p> : <p className="hint">loading…</p>;
  }
  if (lanes.length === 0) {
    return (
      <p className="hint">
        No coordinators discovered yet; the lanes appear once the broker connection sees
        the fleet.
      </p>
    );
  }

  const fleetDelta = report
    ? Object.values(report.instances).reduce(
        (sum, entry) => sum + (entry.steady.after_us_per_s - entry.steady.before_us_per_s),
        0,
      )
    : null;

  return (
    <>
      <div className="toolbar">
        <input
          className="grow"
          placeholder="Search devices and groups…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="segmented">
          {(
            [
              ["all", "All"],
              ["routers", "Routers"],
              ["end_devices", "End devices"],
            ] as Array<[KindFilter, string]>
          ).map(([value, label]) => (
            <button
              key={value}
              className={value === kindFilter ? "seg-btn active" : "seg-btn"}
              onClick={() => setKindFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="segmented">
          {(
            [
              ["spend", "By spend"],
              ["name", "By name"],
            ] as Array<[SortKey, string]>
          ).map(([value, label]) => (
            <button
              key={value}
              className={value === sortKey ? "seg-btn active" : "seg-btn"}
              onClick={() => setSortKey(value)}
            >
              {label}
            </button>
          ))}
        </div>
        {pricing && <span className="hint">pricing…</span>}
      </div>
      {error && <p className="error">{error}</p>}
      <div className="lanes">
        {lanes.map((base) => (
          <div key={base} className="lane panel">
            {laneHeader(base, context.instances[base])}
            {laneBody(base, context.instances[base])}
          </div>
        ))}
      </div>
      <div className="panel">
        <p className="panel-kicker">Staged moves</p>
        {moves.length === 0 ? (
          <p className="hint">
            Drag a device or group to another coordinator's lane, or use a chip's
            "Move to…" control. Predictions reprice the recorded traffic; nothing here
            touches the mesh.
          </p>
        ) : (
          <>
            {moves.map((move, index) => trayLine(move, index))}
            {report && report.channel_pools.length > 0 && (
              <p className="hint">
                {report.channel_pools
                  .map(
                    (pool) =>
                      `${pool.instances.join(" and ")} share channel ${pool.channel}: ` +
                      `their combined staged load is ${usPerS(pool.combined_after_us_per_s)} ` +
                      `(${pool.combined_after_pct_of_budget.toFixed(1)}% of one channel budget)`,
                  )
                  .join("; ")}
              </p>
            )}
            <div className="toolbar">
              {fleetDelta !== null && (
                <span
                  className="chip"
                  title="Sum of every coordinator's staged steady change; router census shifts can make a move cost more than it frees"
                >
                  fleet steady {fleetDelta > 0 ? "+" : ""}
                  {usPerS(fleetDelta)}
                </span>
              )}
              <button
                className="ghost"
                onClick={() => setMoves((current) => current.slice(0, -1))}
              >
                Undo last
              </button>
              <button className="ghost" onClick={() => setMoves([])}>
                Reset
              </button>
              <button
                className="ghost"
                disabled={scoring || moves.length === 0}
                title="Run the rebalancing advisor's acceptance rule over these staged moves: does every pressured coordinator clear without pushing another past its limits?"
                onClick={() => void scoreNow()}
              >
                {scoring ? "Scoring…" : "Score with the advisor"}
              </button>
              <button
                className="ghost"
                disabled={exporting || moves.length === 0}
                title="Download the migration manifest: a versioned JSON plan for your own tooling, with each move's predicted numbers embedded as the receipts later verification measures against"
                onClick={() => void exportManifest()}
              >
                {exporting ? "Exporting…" : "Export migration manifest"}
              </button>
              {Array.from(new Set(moves.map((move) => move.to_instance))).map((dest) => (
                <button
                  key={dest}
                  className="ghost"
                  disabled={replayBusy}
                  title="Reproduce the staged scenario's recomposed burst on this coordinator with benign reads (nothing actuates) and watch the real latency under it. Each run needs its own authorization; the record lands on the Calibration view."
                  onClick={() => void previewReplay(dest)}
                >
                  Verify live on {dest}…
                </button>
              ))}
            </div>
            {replayStarted && (
              <p className="hint">
                Replay started: live progress and the record are on the Calibration view.
              </p>
            )}
            {replayPreview && (
              <div className="banner">
                <strong>
                  Controlled replay on {replayInstance}: authorize to transmit.
                </strong>
                <p className="hint">{String(replayPreview.traffic)}</p>
                {((replayPreview.warnings as string[] | undefined) ?? []).map(
                  (warning) => (
                    <p key={warning} className="hint">
                      <span className="chip warn">note</span> {warning}
                    </p>
                  ),
                )}
                <div className="toolbar">
                  <button disabled={replayBusy} onClick={() => void authorizeReplay()}>
                    {replayBusy ? "Starting…" : "Authorize this replay"}
                  </button>
                  <button
                    className="ghost small"
                    onClick={() => setReplayPreview(null)}
                  >
                    Cancel
                  </button>
                  <span className="hint">
                    Single-use authorization; nothing persists across runs.
                  </span>
                </div>
              </div>
            )}
            {score && (
              <div className={score.accepted ? "banner ok" : "banner"}>
                <strong>
                  {score.accepted
                    ? "The advisor accepts this scenario."
                    : "The advisor would not propose this scenario."}
                </strong>
                <ul>
                  {score.notes.map((note, index) => (
                    <li key={index} className="hint">
                      {note}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
      <div className="panel">
        <p className="panel-kicker">Saved scenarios</p>
        <div className="toolbar">
          <input
            placeholder="Scenario name"
            value={saveName}
            onChange={(event) => setSaveName(event.target.value)}
          />
          <button
            className="ghost"
            disabled={!saveName.trim() || moves.length === 0}
            title="Keep the staged move list on the server under this name"
            onClick={() => void saveScenario()}
          >
            Save staged moves
          </button>
        </div>
        {saved.length === 0 ? (
          <p className="hint">No saved scenarios yet.</p>
        ) : (
          saved.map((scenario) => (
            <div key={scenario.name} className="row">
              <span className="grow">
                <strong>{scenario.name}</strong> · {scenario.moves.length} move
                {scenario.moves.length === 1 ? "" : "s"} · saved{" "}
                {new Date(scenario.saved_at * 1000).toLocaleString()}
              </span>
              <button className="ghost small" onClick={() => setMoves(scenario.moves)}>
                Load
              </button>
              <button
                className="ghost small"
                onClick={() => void deleteScenario(scenario.name)}
              >
                Delete
              </button>
            </div>
          ))
        )}
      </div>
      <p className="hint">
        Everything on this page is read-only analysis of recorded traffic: predictions
        reprice what was actually observed, and whether a moved device can reach its new
        coordinator by radio is always unknown until you try. Applying a plan is your
        tooling's job; zigbee-ninja never writes to the mesh, the broker, or the
        controller.
      </p>
    </>
  );
}
