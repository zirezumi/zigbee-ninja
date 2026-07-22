import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  CostCompletionDelay,
  CostDestinationLoad,
  CostNone,
  CostPublishDelta,
  CostStaleness,
  Recommendation,
  RecommendationCost,
  RecommendationSignificance,
  RecommendationsView,
} from "../api";

// Detector identifiers stay stable in the API; the GUI translates them.
const DETECTOR_LABELS: Record<string, string> = {
  pacing: "Command pacing",
  groupcast_economics: "Group economics",
  redundancy: "Duplicate commands",
  reporting: "Device reporting",
  rebalancing: "Coordinator rebalancing",
  retry_hotspots: "Retry hotspots",
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
  retry_hotspots:
    "Coordinators retrying an outsized share of their transmissions, with the weak links and busy relays most likely responsible; low confidence by nature",
};

/** Significance bands (`recommend/significance.py`). The band answers a
 * different question from the saving: not how much a change frees, but whether
 * anything was waiting on what it frees. */
const BAND_LABELS: Record<string, string> = {
  high: "relieves real pressure",
  moderate: "relieves some pressure",
  low: "relieves nothing today",
  unknown: "relief unknown",
};

const BAND_HINTS: Record<string, string> = {
  high:
    "The budget this change frees is genuinely busy, and the change removes a large share of what is being spent on it.",
  moderate:
    "The budget this change frees is busy, but the change removes only a small share of what is being spent on it.",
  low:
    "The change would free real capacity, but almost nothing is being spent on that budget today, so freeing it wins nothing right now. It stays in the queue and moves up on its own if traffic grows.",
  unknown:
    "How busy that budget currently is has not been measured on this coordinator, so this finding is neither promoted nor demoted. A capacity benchmark on the Calibration page fills the gap.",
};

/** Every budget a finding can save or spend on, glossed in plain language.
 * They are not interchangeable, which is the whole point of the cost block:
 * a change that frees one can be paid for out of another. Keys mirror the
 * denominator vocabulary in `recommend/cost.py`, which is its single home;
 * a constant added there needs its gloss added here in the same change. */
const DENOMINATOR_GLOSS: Record<string, string> = {
  "channel airtime":
    "The share of the radio channel's time this coordinator's traffic occupies.",
  "command rate":
    "How many commands per second the coordinator has to push out. This is the budget the measured capacity limit binds, and it is not the same budget as channel airtime.",
  "peak command rate":
    "The busiest single second recorded, rather than the average. Bursts are what run into the capacity limit; averages hide them.",
  "device service rate":
    "How fast one device can be commanded before its own requests start queueing behind each other. The capacity benchmark measures it per device.",
  "state staleness":
    "How soon the rest of your system learns that a device's state changed. Slowing a device's reports saves traffic and is paid for in delay.",
  "burst completion time":
    "How long a burst of commands takes to finish. Spreading a burst adds no traffic at all; it just takes longer to complete.",
};

const COSTS_NOTHING_HINT =
  "This action adds and removes nothing on any measured budget. It is stated rather than left blank so a change that is genuinely free reads differently from one whose cost nobody worked out.";

const LOW_BAND_HINT =
  "Findings whose saving is real but lands on a budget that is barely used, so there is nothing to relieve today. They are collapsed rather than dropped: everything the detectors found stays available, and a finding moves out of this group by itself once its budget comes under pressure.";

const STATE_TABS: Array<[string, string]> = [
  ["open", "Open"],
  ["dismissed", "Dismissed"],
  ["applied", "Applied"],
  ["verified", "Verified"],
  ["regressed", "Regressed"],
  ["all", "All"],
];

function when(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

/** The API serves rationale and note strings as sentence bodies (lowercase,
 * no full stop) so callers can embed them; a card shows them as sentences. */
function sentence(text: string | null | undefined): string {
  const trimmed = (text ?? "").trim();
  if (!trimmed) return "";
  const capitalized = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capitalized) ? capitalized : `${capitalized}.`;
}

function count(value: number): string {
  return Math.round(value).toLocaleString();
}

/** One decimal, no unit: every call site spells the unit out in prose. */
function round1(value: number): string {
  return `${Math.round(value * 10) / 10}`;
}

/** A duration a person has to feel, in the unit they would use for it. */
function seconds(value: number): string {
  if (value < 90) return `${Math.round(value * 10) / 10} s`;
  if (value < 5400) return `${Math.round(value / 60)} min`;
  return `${Math.round(value / 3600)} h`;
}

function millis(value: number): string {
  return value >= 1000 ? `${Math.round(value / 100) / 10} s` : `${Math.round(value)} ms`;
}

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
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

/** Whether the saving is worth having: the size of the saving weighed against
 * how busy the budget it frees actually is. Detectors that do not assess it
 * serve an empty object, and then the card says nothing rather than guessing. */
function SignificanceLine({
  significance,
}: {
  significance: RecommendationSignificance;
}) {
  const band = significance.band;
  if (!band) return null;
  const relief = significance.relief_pct;
  const reliefHint =
    relief === null || relief === undefined
      ? ""
      : relief >= 100
        ? " It is modeled to remove essentially everything spent on that budget today."
        : ` It removes about ${relief}% of what is spent on that budget today.`;
  // The rationale names its budget ("the peak command rate budget"), which is
  // the one piece of jargon in the sentence, so the gloss rides the same chip.
  const denominator = significance.denominator;
  const gloss = denominator ? DENOMINATOR_GLOSS[denominator] : undefined;
  const glossHint = gloss ? ` ${denominator}: ${gloss}` : "";
  return (
    <p className="hint">
      <span
        className={band === "high" ? "chip ok" : "chip"}
        title={(BAND_HINTS[band] ?? "") + reliefHint + glossHint}
      >
        {BAND_LABELS[band] ?? band}
      </span>{" "}
      {sentence(significance.rationale)}
    </p>
  );
}

/** `kind: "none"`: the trade was assessed and there was nothing on the other
 * side of it. Quiet and neutral, but never dropped, because "priced, and free"
 * has to read differently from "nobody priced this". */
function CostFreeLine({ cost }: { cost: CostNone }) {
  return (
    <p className="hint">
      <span className="chip" title={COSTS_NOTHING_HINT}>
        costs nothing on the mesh
      </span>{" "}
      {sentence(cost.note)}
    </p>
  );
}

/** `publish_delta`: the action changes how many commands go out. */
function PublishDeltaDetail({ cost }: { cost: CostPublishDelta }) {
  const before = cost.publishes_before;
  const after = cost.publishes_after;
  const multiplier = cost.publish_multiplier;
  const perDay = cost.delta_commands_per_day;
  const perSecond = cost.delta_eps_mean;
  const peak = cost.measured_peak_eps;
  const limit = cost.capacity_limit_eps;
  const peakPct =
    peak !== null && peak !== undefined && limit ? Math.round((peak / limit) * 100) : null;
  return (
    <>
      {before !== undefined && after !== undefined && before !== after && (
        <p>
          {count(before)} recorded {plural(before, "command", "commands")} become{" "}
          {count(after)}
          {multiplier && multiplier > 1 ? ` (${round1(multiplier)} times as many)` : ""}
          {perDay
            ? `, about ${count(Math.abs(perDay))} ${perDay > 0 ? "more" : "fewer"} per day`
            : ""}
          {perSecond
            ? ` (${Math.abs(perSecond).toFixed(2)} per second averaged over the window)`
            : ""}
          .
        </p>
      )}
      {peak !== null && peak !== undefined && (
        <p>
          {limit
            ? `This coordinator already peaks at ${round1(peak)} commands per second` +
              (peakPct !== null
                ? `, ${peakPct}% of its measured capacity limit of ${round1(limit)} per second.`
                : ".")
            : `This coordinator already peaks at ${round1(peak)} commands per second; no capacity benchmark has run here, so there is no measured limit to judge that against.`}
        </p>
      )}
    </>
  );
}

/** `destination_load`: the relief is bought on the coordinator that receives
 * the move, so the peak is quoted against that coordinator's own limit. */
function DestinationLoadDetail({ cost }: { cost: CostDestinationLoad }) {
  const before = cost.peak_eps_before;
  const after = cost.peak_eps_after;
  const limit = cost.capacity_limit_eps;
  const pctAfter = cost.peak_pct_of_limit_after;
  const ceilingHint = cost.ceiling_eps
    ? `The fastest this coordinator ever ran during a benchmark was ${round1(cost.ceiling_eps)} commands per second; past that, commands stall until the queue drains.`
    : undefined;
  if (before === undefined || after === undefined) return null;
  return (
    <p title={ceilingHint}>
      {`${cost.destination_instance ?? "The receiving coordinator"} goes from a recorded peak of ` +
        `${round1(before)} commands per second to ${round1(after)}` +
        (pctAfter !== null && pctAfter !== undefined && limit
          ? `, ${pctAfter}% of its own measured capacity limit of ${round1(limit)} per second.`
          : ".")}
    </p>
  );
}

/** `completion_delay`: nothing extra is sent, the burst just takes longer. */
function CompletionDelayDetail({ cost }: { cost: CostCompletionDelay }) {
  const added = cost.added_completion_ms;
  const commands = cost.commands_in_burst;
  if (added === undefined) return null;
  const same =
    commands === undefined
      ? "The burst sends exactly what it sends today"
      : `The same ${count(commands)} ${plural(commands, "command", "commands")} still go out`;
  return (
    <p>
      {added === 0
        ? `${same}, and the burst finishes no later than it does today.`
        : `${same}; the burst just takes about ${millis(added)} longer to finish.`}
    </p>
  );
}

/** `staleness`: the reports stop, so everything watching this device hears
 * about a change later. That delay is the whole price of the action. */
function StalenessDetail({ cost }: { cost: CostStaleness }) {
  const now = cost.mean_interval_s_now;
  const reference = cost.mean_interval_s_at_reference;
  const added = cost.added_delay_s;
  const perDayNow = cost.reports_per_day_now;
  const perDayReference = cost.reports_per_day_at_reference;
  return (
    <>
      {cost.presence_hardware && (
        <p>
          <span
            className="chip warn"
            title="This device looks like presence, motion, or occupancy hardware. Added reporting delay is felt first on these: whatever reacts to the device reacts later. Choose the new interval against what consumes its state."
          >
            presence hardware
          </span>
        </p>
      )}
      {now !== null && now !== undefined && reference !== null && reference !== undefined && (
        <p>
          {`One report every ${seconds(now)} becomes one every ${seconds(reference)}` +
            (added !== null && added !== undefined
              ? `, so a change in this device's state reaches everything watching it up to ${seconds(added)} later.`
              : ".")}
        </p>
      )}
      {perDayNow !== undefined &&
        perDayReference !== undefined &&
        perDayReference < perDayNow && (
          <p>{`About ${count(perDayNow)} reports a day becomes about ${count(perDayReference)}.`}</p>
        )}
    </>
  );
}

/** Detail lines for one cost kind, dispatched on the discriminator rather than
 * sniffed from which keys are present. A kind this build has never seen adds
 * no lines and falls through to its authored note, which is always rendered. */
function CostDetail({ cost }: { cost: RecommendationCost }) {
  switch (cost.kind) {
    case "publish_delta":
      return <PublishDeltaDetail cost={cost} />;
    case "destination_load":
      return <DestinationLoadDetail cost={cost} />;
    case "completion_delay":
      return <CompletionDelayDetail cost={cost} />;
    case "staleness":
      return <StalenessDetail cost={cost} />;
    default:
      return null;
  }
}

/** Whether the trade was priced at all. An empty block means no detector
 * assessed it, which must read differently from a priced-and-free `none`. */
function hasCost(cost: RecommendationCost): boolean {
  return cost.kind !== undefined || Boolean(cost.note);
}

/** The action would change behavior, not just cost.
 *
 * A detector can establish that its action is cheaper and still not be able to
 * promise it does the same thing. Retargeting a group to per-member commands
 * is the case that exists today: a command addressed to a device traverses
 * that device's own Zigbee bindings, while the same command addressed to a
 * group does not, so a member with bindings makes the two genuinely different
 * operations. That is a stronger reason to refuse than any cost, because no
 * amount of headroom makes it safe, and it can be true of a change that adds
 * no load at all (a one-member group sends the same number of commands either
 * way). So it renders above the cost block and never depends on it. */
function NotEquivalentWarning({ action }: { action: Record<string, unknown> }) {
  if (action.behavior_neutral !== false) return null;
  const bound = Array.isArray(action.bound_members)
    ? (action.bound_members as string[])
    : [];
  return (
    <div className="cost-note bad">
      <p>
        <span
          className="chip bad"
          title="Applying this would not do the same thing by a cheaper route; it would change what the command reaches. Check why the current arrangement exists before accepting."
        >
          not equivalent
        </span>{" "}
        This is not a cheaper way to do the same thing: it changes what the
        command reaches.
        {bound.length > 0 && (
          <>
            {" "}
            {bound.length === 1 ? "This device has" : "These devices have"} their
            own Zigbee bindings:{" "}
            <span className="mono">{bound.join(", ")}</span>.
          </>
        )}
      </p>
    </div>
  );
}

/** What the change costs on the budgets it does not save on.
 *
 * Shown for every priced trade, not only the ones that add load: a cost paid
 * in delay is still a cost, and a reader deciding whether to accept a change
 * needs it. `raises_load` drives how the block reads (a warning rather than a
 * neutral fact), and `raises_load` on a `low`-band finding is the product's
 * own verdict that the trade is a bad one. */
function CostBlock({ cost, badTrade }: { cost: RecommendationCost; badTrade: boolean }) {
  const denominator = cost.denominator;
  const raisesLoad = cost.raises_load === true;
  return (
    <div className={badTrade ? "cost-note bad" : raisesLoad ? "cost-note warn" : "cost-note"}>
      <p>
        <span className={badTrade ? "chip bad" : raisesLoad ? "chip warn" : "chip"}>
          {badTrade ? "bad trade" : raisesLoad ? "costs more elsewhere" : "what it costs"}
        </span>{" "}
        {badTrade
          ? "This frees a budget that nothing is waiting on, and pays for it by raising one that is measured and finite."
          : raisesLoad
            ? "This saving is paid for by raising a different budget."
            : "This adds no traffic anywhere; what it costs is paid on a different budget."}{" "}
        {denominator && (
          <>
            {raisesLoad ? "It raises the " : "It is paid for in "}
            <span title={DENOMINATOR_GLOSS[denominator]}>{denominator}</span>.
          </>
        )}
      </p>
      <CostDetail cost={cost} />
      {cost.note && <p className="hint">{sentence(cost.note)}</p>}
    </div>
  );
}

/** §V2-6 receipts: what the measured before/after windows actually said. */
function VerificationBlock({
  verification,
}: {
  verification: NonNullable<Recommendation["verification"]>;
}) {
  const verdictLabels: Record<string, [string, string]> = {
    improved: ["improved", "chip ok"],
    regressed: ["regressed", "chip bad"],
    no_material_change: ["no material change yet", "chip"],
    pending: ["verifying…", "chip"],
  };
  const [label, className] = verdictLabels[verification.verdict] ?? [
    verification.verdict,
    "chip",
  ];
  const numbers =
    verification.before_us_per_day !== undefined
      ? `${Math.round(verification.before_us_per_day)} → ${Math.round(
          verification.after_us_per_day ?? 0,
        )} µs/day over ${verification.before_days}+${verification.after_days} completed days`
      : verification.before_peak_eps !== undefined
        ? `peak ${verification.before_peak_eps}/s → ${verification.after_peak_eps}/s` +
          (verification.sustained_limit_eps
            ? ` (limit ${verification.sustained_limit_eps}/s)`
            : "")
        : null;
  return (
    <p className="hint" title={verification.basis ?? undefined}>
      <span className={className}>{label}</span>{" "}
      {verification.metric ? `${verification.metric}: ` : ""}
      {numbers ?? verification.note ?? ""}
      {verification.finalized
        ? " · watched for two weeks with no material change; the check has stopped"
        : ""}
    </p>
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
  const [exporting, setExporting] = useState(false);
  // The product's own verdict on the trade: a change that raises a measured,
  // finite budget to relieve one nobody is waiting on is a bad deal, and the
  // card has to say so rather than leaving the reader to notice.
  const badTrade = rec.cost.raises_load === true && rec.significance.band === "low";
  const rebalanceMoves =
    rec.detector === "rebalancing" && Array.isArray(rec.action.moves)
      ? (rec.action.moves as Array<Record<string, unknown>>)
      : null;

  async function copyCard() {
    const copied = await copyText(copyPayload(rec));
    setCopyLabel(copied ? "Copied" : "Copy failed");
    window.setTimeout(() => setCopyLabel("Copy"), 1500);
  }

  async function exportManifest() {
    if (!rebalanceMoves) return;
    setExporting(true);
    try {
      const manifest = await api<Record<string, unknown>>("/api/scenario/manifest", {
        method: "POST",
        body: JSON.stringify({ moves: rebalanceMoves, source: "advisor" }),
      });
      const blob = new Blob([JSON.stringify(manifest, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `migration-manifest-${rec.instance}-${new Date()
        .toISOString()
        .slice(0, 10)}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
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
      <NotEquivalentWarning action={rec.action} />
      <SignificanceLine significance={rec.significance} />
      {rec.cost.kind === "none" ? (
        <CostFreeLine cost={rec.cost} />
      ) : hasCost(rec.cost) ? (
        <CostBlock cost={rec.cost} badTrade={badTrade} />
      ) : null}
      {rec.state_note && <p className="hint">Note: {rec.state_note}</p>}
      {rec.verification && <VerificationBlock verification={rec.verification} />}
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
        {(rec.state === "dismissed" ||
          rec.state === "applied" ||
          rec.state === "verified" ||
          rec.state === "regressed") && (
          <button className="ghost" onClick={() => onState(rec, "open")}>
            Reopen
          </button>
        )}
        {rec.state === "regressed" && (
          <button className="ghost" onClick={() => onState(rec, "dismissed")}>
            Dismiss
          </button>
        )}
        <button
          className="ghost"
          title="Copy this recommendation as JSON: detector, coordinator, finding text, saving, and evidence"
          onClick={() => void copyCard()}
        >
          {copyLabel}
        </button>
        {rebalanceMoves && (
          <button
            className="ghost"
            disabled={exporting}
            title="Download this proposal as a migration manifest: a versioned JSON plan for your own tooling, with the predicted numbers embedded as verification receipts"
            onClick={() => void exportManifest()}
          >
            {exporting ? "Exporting…" : "Export manifest"}
          </button>
        )}
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

  // The API ranks the queue by significance band and then by the older
  // saving × confidence term (`recommend/store.py` `_rank`); this view must
  // never re-sort it. Filtering walks the list once, so both groups keep the
  // server's order exactly.
  const queue = view?.recommendations ?? [];
  const pressing = queue.filter((rec) => rec.significance.band !== "low");
  const lowBand = queue.filter((rec) => rec.significance.band === "low");
  const lowBandBadTrades = lowBand.filter((rec) => rec.cost.raises_load).length;

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
        <>
          {pressing.length === 0 && (
            <div className="panel">
              <p>Nothing in this list would relieve anything today.</p>
              <p className="hint">
                Every finding here landed in the group below: the budgets they would
                free are barely used. They stay listed, and one moves up on its own
                as soon as its budget comes under pressure.
              </p>
            </div>
          )}
          {pressing.map((rec) => (
            <Card key={rec.id} rec={rec} onState={(r, s) => void setState(r, s)} />
          ))}
          {lowBand.length > 0 && (
            // Keyed on the tab so switching tabs returns the group to its
            // collapsed default; nothing is dropped, only folded away.
            <details key={stateFilter} className="low-band">
              <summary className="hint" title={LOW_BAND_HINT}>
                {lowBand.length} {plural(lowBand.length, "finding", "findings")} that
                would relieve nothing today
                {lowBandBadTrades > 0 && (
                  <>
                    {" "}
                    <span
                      className="chip bad"
                      title="These would also raise a budget that is measured and finite, to relieve one nobody is waiting on: the product's own verdict is that they are bad trades."
                    >
                      {lowBandBadTrades} would raise load
                    </span>
                  </>
                )}
              </summary>
              <p className="hint">
                Nothing is hidden here. The savings are real, but the budgets they
                free are barely used, so freeing them wins nothing right now. Each
                one says what it would free and what it is currently worth.
              </p>
              {lowBand.map((rec) => (
                <Card key={rec.id} rec={rec} onState={(r, s) => void setState(r, s)} />
              ))}
            </details>
          )}
        </>
      )}
      <p className="hint">
        Recommendations are ordered by what they would actually relieve: findings on a
        budget that is currently under pressure come first, and within each group the
        older ordering (estimated saving weighted by confidence) still applies. Savings
        are comparable estimates in the same currency as Top spenders, not meter
        readings; each card's evidence says exactly what was measured and what was
        modeled. Applying a change is always your tooling's job: zigbee-ninja never
        writes to the mesh, the broker, or the controller.
      </p>
    </>
  );
}
