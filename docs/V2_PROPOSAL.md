# zigbee-ninja V2: the optimization loop

| | |
|---|---|
| **Status** | RATIFIED 2026-07-16: V2.M1 green-lit; §V2-10 resolved below |
| **Date** | 2026-07-15 (drafted) · 2026-07-16 (ratified) |
| **Depends on** | DESIGN.md (V1, canonical): everything there stays true |

V1 answers *"how much of each coordinator's capacity is used, and by what."*
V2 answers the question that follows: ***"what, precisely, should the
installation change: and did the change work?"***

The product stance does not move: zigbee-ninja **never touches the mesh or the
controller**. V2's output is recommendations with modeled savings and, after
the user applies a change through their own tooling, a measured verdict. The
loop is: **measure → attribute → recommend → (user applies) → verify**.
Closing it turns a monitoring tool into a continuous traffic-cost regression
suite for a Zigbee installation: the same way CI turns tests from a one-off
audit into a standing guarantee.

Everything below is generic core (P6): derived from discovery, registries,
and observed traffic. The author's reference deployment is the first user,
never a special case.

## §V2-1 Why this is the right V2

Three V1 facts make the loop buildable now, not speculative:

1. **Attribution already names the spender.** Chains carry the commanding
   automation/script/user (HA integration) or MQTT client. Nothing new is
   needed to say *who* causes traffic: only to price it.
2. **The airtime model already prices frames.** Per-frame µs with unicast
   hop/retry structure and groupcast mesh amplification exists; pricing a
   *chain* is a join away. Every price inherits a provenance tag (P5).
3. **The raw event store holds ~48 h of ground truth.** Recommendations do
   not have to argue from theory: a proposed change can be **re-costed
   against the recorded traffic** ("your last 24 h, replayed under the
   proposed grouping, would have cost 31% less airtime"). DuckDB over the
   §12 store makes counterfactual replay a query, not a simulator project.

## §V2-2 New core: the cost ledger

A per-chain **airtime cost** joins the attribution dimensions:

```text
chain_cost_us = Σ TX frames (unicast: hops × (frame+ACK+IFS) × (1+retry_rate)
                            groupcast: (1+N_routers) × frame × avg_tx)
              + Σ provoked RX frames (final hop, per §10 rules)
```

- Persisted on finalized chains; rolled up per (instance, commander, day)
  and per (instance, device, day): the **ledger**.
- Autonomous (non-commanded) traffic is priced too, per device: reporting
  cost is real cost and often dominant on sensor-heavy meshes.
- Every ledger row carries the weakest provenance tag among its inputs
  (`reconstructed` at best from the wire tier, `inferred` at T0-only) plus
  the model parameters used (avg_tx, retry factor, hop assumption), so a
  later parameter improvement is distinguishable from a real traffic change.
- Self-traffic stays self-attributed (P4) and is shown in the ledger like
  any other spender: zigbee-ninja pays rent in its own books.

The Attribution explorer grows cost columns; a **Top spenders** panel ranks
commanders and devices by µs/day with trend arrows. This ledger is the
currency every V2 surface trades in.

> **Implementation (V2.M1):** finalized chains are priced at the 10 s flush
> (`capacity/ledger.py`), using the instance's measured avg_tx and MAC retry
> rate once counter windows have produced them and the §10 defaults before
> that; each row records which was used. Rollups land in `ledger_daily` and
> `ledger_device_daily` (µs, 365-day retention) with provenance and pricing
> parameters. Autonomous state publishes are priced per device at a modeled
> report size. zigbee-ninja's own mesh commands (benchmark reads) are priced
> under the `zigbee-ninja` commander; their state echoes are consumed by the
> calibration engine and stay unpriced, so self rows are TX-only lower
> bounds, consistent with §10's posture. `GET /api/ledger` serves the
> rollup: because the ledger is daily, a window rounds out to whole UTC
> days, and every rate divides by the elapsed wall clock since the earliest
> returned day began, bounded by when ledger recording started (the
> response states all three). Group state topics are excluded from
> per-device costing: they are Zigbee2MQTT's synthetic optimistic state,
> not mesh frames. `GET /api/journal` serves the change journal.

## §V2-3 Change journal (the loop's clock)

zigbee-ninja already watches `bridge/info`, `bridge/devices`, and
`bridge/groups` continuously. V2 derives a passive **change journal**: a
timestamped record whenever the installation itself changes:

- device joined / left / **moved between instances**
- group created / deleted / **membership changed**
- Z2M version, coordinator firmware, **channel** changed
- controller integration connected / reconfigured

Journal entries are regime boundaries. They (a) annotate every time series
("what happened at 14:32?"), (b) delimit before/after windows for
verification (§V2-6), (c) generalize the existing "recalibrate?" nudge, and
(d) give recommendations a natural "was it applied?" detector for the many
changes that are visible in registries. Zero new footholds; pure derivation
from what T0 already sees.

> **Implementation (V2.M1):** the registry diffs each `bridge/devices` /
> `bridge/groups` / `bridge/info` refresh against what it already held:
> device added / removed / renamed / rejoined (network address change),
> group added / removed / renamed / membership changed, Z2M version /
> channel / coordinator firmware changed. The first sight of an instance
> after boot is a baseline, so retained republishes and collector restarts
> journal nothing. A device added within a day of its removal from a
> different instance is annotated `moved_from`: the move-between-instances
> signal. Rows persist in `journal` (90-day retention). Controller-link
> events (HA connected / reconfigured) are a later addition.

## §V2-4 Budgets & regression watch

The ledger makes cost a first-class metric, so the existing alert engine
(§14) extends naturally:

- **Baselines:** per-commander and per-device rolling cost baselines
  (e.g. 14-day median of µs/day, min-history-gated).
- **Regression rules:** "commander cost > K× its baseline sustained N
  evaluation windows" → alert naming the automation, with the ledger rows as
  evidence. The canonical V2 alert: *"⟨automation X⟩ got 40% chattier after
  yesterday's controller change."*
- **Budgets:** optional explicit caps per commander/instance (µs/s or % of
  channel budget), for users who want CI-style hard limits rather than
  drift detection. Seeded disabled, like all capacity rules.

New metrics ride the existing rule/state machinery: freeze-on-missing-data,
restart rebaselining, and seed-once semantics all apply unchanged.

> **Implementation (V2.M2):** baselines are rolling 14-day medians of
> µs/day per commander and per device, computed from the daily ledger over
> completed recording days (a day the ledger never saw is not history; a
> spender silent on a recording day cost zero that day), gated on three
> completed days and on a nonzero median so fresh deployments and
> never-before-seen spenders freeze rather than alert. Four metrics ride
> the evaluator: `commander_cost_ratio` and `device_cost_ratio` (trailing
> 24 h spend over the median; rules key their state machines on spender
> names, so one `'*'` rule watches every commander or device) plus
> `commander_cost_us_per_s` and `instance_cost_us_per_s` for explicit
> budgets. Regression rules seed disabled at the ratified 2x-over-median,
> 24 h-sustain defaults; budget rules seed disabled with placeholder
> thresholds. zigbee-ninja's own spend is excluded from the regression
> ratio (benchmark runs are operator-initiated, not drift) but stays
> visible to budget rules. The Top spenders panel shows the same ratio as
> a per-row trend at the row's instance grain, and Fleet rows carry the
> per-instance ledger cost line.

## §V2-5 The recommendation engine

A set of **detectors**, each emitting structured findings:

```json
{
  "id": "rec-…", "detector": "groupcast_economics", "instance": "…",
  "finding": "…human-readable…",
  "action": { "kind": "regroup|retarget|pace|rebalance|reconfigure_reporting|dedupe", "…machine-readable params…" },
  "saving": { "us_per_s": 123.4, "pct_of_budget": 1.2,
              "basis": "replayed 24 h of recorded traffic",
              "provenance": "reconstructed|modeled" },
  "confidence": "high|medium|low",
  "evidence": ["chain ids / window refs / journal refs"],
  "state": "open | dismissed | applied | verified | regressed"
}
```

Principles: every saving is **counterfactual-replayed** against the raw
store where possible (basis says so; anything else is `modeled` and says
that instead); every recommendation carries confidence and evidence links;
dismissals are durable (a dismissed finding never nags again unless its
inputs materially change).

**Detector inventory, first wave: ordered by expected value on real meshes:**

1. **Redundant-command costing.** The V1 detector already finds identical
   commands to the same target inside a window; V2 groups clusters by
   commander, prices them with the ledger, and emits "⟨automation⟩ resends
   identical state ~N×/day ≈ M µs/s of airtime" with the dedupe action.
2. **Groupcast economics.** Both directions, from recorded traffic:
   *(a)* fan-outs of near-simultaneous unicasts to the same member set that
   a group command would collapse (replay: unicast cost vs amplified
   groupcast cost on this mesh's router census and measured avg_tx);
   *(b)* groups so small or so router-adjacent that per-member unicast
   would be cheaper than the broadcast amplification; *(c)* membership
   pruning: members whose state never diverges from another group's.
3. **Reporting-configuration advisor.** Autonomous reporting is priced per
   device in the ledger; devices whose reporting dominates their class
   (e.g. a sensor publishing every trivial delta) get "raise min-interval /
   delta" recommendations with replayed savings. On many installations this
   is the single largest recoverable cost, and it is invisible without
   per-device autonomous costing.
4. **Pacing advisor.** Burst microscopy over the raw store finds commanders
   whose bursts exceed the *measured* per-device service rate (queueing
   latency, from single-target calibration) or approach the *measured*
   coordinator knee (from spread calibration). Because the latency-vs-load
   scatter is measured, the predicted p95 improvement from spreading a
   burst is interpolation on this mesh's own curve: honest within the
   observed load range, `modeled` beyond it. Output: "stagger these N
   commands over ≥T ms" with the specific automations named.
5. **Rebalancing advisor (what-if).** The per-device ledger + per-instance
   knees + channel map feed a solver over *user-proposed or auto-suggested*
   moves: device→instance reassignments and channel changes. Output is a
   ranked **migration manifest** (machine-readable, schema-stable) any
   controller-side tooling can consume; zigbee-ninja never executes it.
   Savings are replayed: last 24 h of each candidate device's traffic
   re-costed on the destination's router census, channel pooling, and
   measured amplification factor.

   Its core is **burst envelope analysis**: steady-state budgets understate
   real strain, because bursts are what bind a mesh sized to kill multicast
   and delivery errors. A consolidation what-if therefore (a) **overlays the
   recorded per-mesh event streams** from the raw store on a common clock
   and re-costs them under the combined router census, comparing
   fine-grained peaks against the *measured* knees rather than averages;
   (b) composes each automation's **worst observed burst**, including
   plausible co-trigger sets, to bound the theoretical worst case
   statically; and (c) optionally verifies the prediction live with a
   **controlled replay benchmark** on calibration's per-run-authorized
   rails: which also settles empirically whether controller-side command
   staggers are earning their keep.
6. **Retry-cost hotspots.** Per-hop retry rates (validated counters) and
   LQI trends price the *overhead multiplier* per device; chronic
   multipliers get "investigate placement/route" findings. Low confidence
   by construction (radio weather exists); clearly tagged.

**Recommendations view:** a queue ordered by modeled saving × confidence,
each expandable to evidence; empty queue + green budgets is the product's
definition of *"provably traffic-optimized: nothing left that the evidence
supports changing."*

> **Implementation (V2.M3, store + lifecycle):** findings persist in a
> `recommendations` table keyed by a stable id per (detector, instance,
> subject), so re-detection lands on the same row. Detector passes run a
> few minutes after start and hourly after that, off the flush loop on a
> worker thread, and read only persisted stores and registry snapshots.
> An open row a completed detector pass no longer emits is deleted: the
> queue only holds findings the evidence currently supports. A dismissed
> row keeps the content it was dismissed with and reopens only when the
> finding's input fingerprint moves materially (any numeric input changing
> by 1.5×, or a structural change); applied, verified, and regressed rows
> are never touched by detector passes (§V2-6 owns those transitions).
> `GET /api/recommendations` serves the queue ordered by saving ×
> confidence (latency-only findings rank among themselves by predicted
> p95 improvement), `POST /api/recommendations/{id}/state` moves between
> open, dismissed, and applied, and `POST /api/recommendations/run`
> forces a pass. Finalized chains now persist a 12-character payload
> digest (never contents): the identity evidence the groupcast detectors
> join on.
>
> **Implementation (V2.M3, pacing advisor):** bursts are runs of recorded
> command chains with gaps under 1 s. A burst flags when its peak 1 s rate
> reaches 80% of the instance's measured capacity limit (the spread-mode
> knee, read per instance from the calibration records, never assumed) or
> when commands to one device exceed the measured per-device service
> ceiling (the single-target knee). Findings group by (instance,
> commander); a burst without a 60% majority commander reports as
> "(multiple commanders)". The proposed stagger paces the worst burst to
> half the limit. The predicted p95 improvement interpolates the
> latency-vs-load points this mesh has produced (calibration per-step
> curves plus natural 10 s wire-latency rollups); rates beyond every
> observed point report the highest measured point as a floor and tag the
> saving `modeled`. Pacing recovers no airtime, so `saving.us_per_s` is 0
> and the saving is the latency number (`saving.p95_ms`). Confidence
> drops to medium when the knee's calibration environment no longer
> matches the running Zigbee2MQTT or firmware version (the finding then
> says to consider recalibrating), when bursts recur fewer than three
> times, or when only a per-device limit exists to judge aggregate
> pressure against.
>
> **Implementation (V2.M3, groupcast economics):** all three directions
> price recorded chains in the ledger's own currency (fixed ZCL byte
> estimates, the instance's router census, measured avg_tx and MAC retry
> rate when present, defaults otherwise), so savings are comparable with
> the Top spenders numbers. (a) Fan-outs are clusters of same-payload
> unicasts to four or more devices inside 3 s, recurring at least three
> times: joined on the persisted chain payload digest, so only traffic
> recorded after V2.M3 is visible to them. A fan-out that rides beside
> an identical group command is double delivery (a dedupe finding)
> regardless of which shape is cheaper; otherwise a finding appears only
> when the amplified groupcast actually undercuts the unicast sum, and
> retargets to a group with exactly that membership (high confidence)
> or proposes creating one (medium). (b) A group whose per-member
> unicasts undercut its amplified groupcast by at least 30% gets a
> retarget-to-unicast finding, always medium confidence with the loss of
> same-instant member changes stated. (c) A group whose commands arrive
> at least 90% of the time alongside an identical-payload command to a
> containing group is redundant traffic. Findings below 10 µs/s of
> replayed saving stay out of the queue.
>
> **Implementation (V2.M3, redundancy costing):** chains the tracker
> already marked redundant (identical payload to the same target inside
> its window) group by commander and re-price exactly as the ledger
> priced them (registry shape, echo count, measured parameters when
> present), so the saving is the recorded duplicates' own cost in the
> Top spenders currency. High confidence by construction; the same
> 10 µs/s queue floor applies.
>
> **Implementation (V2.M3, reporting advisor):** per-device autonomous
> spend over the trailing day (rates divided by recorded time, exactly
> like `/api/ledger`) compares two ways: against the median of at least
> three other devices with the same vendor and model (a strong
> same-hardware baseline, high confidence), else against the fleet's
> median reporting device at a 5× threshold (medium confidence). A name
> or model suggesting presence sensing (presence, occupancy, motion,
> mmwave, radar, pir) downgrades confidence one step and the finding
> says the stream may be deliberate. Savings replay the recorded volume
> at the reference median; findings under 50 µs/s stay out of the
> queue (reporting is a standing per-device cost, so the bar sits
> higher than the command-shape detectors).

## §V2-6 Verification (what closes the loop)

When a recommendation's change is applied: auto-detected via the journal
where registries show it (regroup, rebalance, membership), or marked
applied by the user where they don't (pacing, controller-side dedupe):
zigbee-ninja opens a verification window:

- **Before** = the ledger/latency/headroom aggregates over N days
  pre-journal-boundary; **after** = the same aggregates post-boundary,
  benchmark windows excluded as in V1.
- Verdict when the after-window has enough data: **improved / no material
  change / regressed**, with the actual deltas (µs/s, % of budget, p95
  latency) and honest guards: minimum window, and a same-hours comparison
  to blunt time-of-day seasonality. No p-value theater: measured deltas
  with stated windows.
- Verdicts feed back: a `regressed` recommendation reopens with its
  real-world result attached; a `verified` one archives with its receipts.
  The Recommendations view becomes a changelog of measured wins.

## §V2-7 Surfaces & packaging

- **Views:** Attribution gains cost columns + Top spenders; new
  **Recommendations** view (queue, evidence, verdicts); Fleet rows gain a
  cost/day line; journal annotations appear on time-series views.
- **API:** `/api/ledger`, `/api/recommendations`, `/api/journal`,
  manifest export.
- **HA entities:** the discovery tile (granted per instance, unchanged
  consent model) adds a `recommendations_open` sensor and a cost/day
  sensor, so controller-side automations can react ("notify me when
  zigbee-ninja finds something").
- Everything stays in the single image (P7); DuckDB does the replay math;
  no new dependencies anticipated beyond what V1 ships. No GPL (P8).

> **Implementation (V2.M3, surfaces):** the Recommendations view is a
> new nav item: state tabs with counts, a scan-now control, cards
> carrying the detector, confidence, and the saving headline with its
> basis in a tooltip, expandable evidence rendered in plain language,
> and dismiss / mark-applied / reopen controls (dismissing offers an
> optional note kept with the row). The open-tab empty state is the
> product's traffic-optimized statement. The discovery publisher's
> sensor set gains `recommendations_open` (a bare count, no unit) per
> granted instance; configs republish on the payload change with
> unique_ids stable, per the granted-tile consent model (§V2-10.5).
> The cost/day sensor named above remains future work.

## §V2-8 What V2 explicitly does not do

- **No write path to the mesh, broker, or controller: not even opt-in.**
  Applying changes is the user's tooling's job; the manifest/contract is
  the boundary. (T2b inline proxy remains the only sanctioned datapath
  exception product-wide, and it is unrelated to V2.)
- **No auto-tuning loops.** A recommendation whose application spawns new
  recommendations converges only because a human gates each step.
- **No cross-installation telemetry.** Baselines are local; no phone-home.

## §V2-9 Sequencing

| Milestone | Deliverable | Proves |
|---|---|---|
| V2.M1 | Cost ledger (chain pricing, rollups, autonomous device costing) + change journal + Attribution cost columns | The currency is sound; regimes are visible |
| V2.M2 | Budgets & regression watch on the alert engine + Top spenders | Cost is a standing guarantee, not a report |
| V2.M3 | Recommendation engine w/ detectors 1–4 + Recommendations view + counterfactual replay | The tool says what to change, with receipts |
| V2.M4 | Rebalancing advisor w/ burst envelope analysis + migration manifest + applied-change verification | The loop closes: recommend → apply → verified delta |

Each milestone dogfoods on the reference deployment before the next starts,
per V1 practice.

## §V2-10 Ratified decisions (owner, 2026-07-16)

1. **Units:** both; the ledger stores µs, displays headline % of channel
   budget with µs/s alongside.
2. **Regression sensitivity:** start loose; default 2× over a 14-day
   median with 24 h sustain, tunable per rule like every other alert.
3. **Manifest contract:** ratified early; the JSON shape above is the
   contract controller-side tooling builds against; field renames from here
   on are breaking changes.
4. **Applied-detection:** hybrid; journal-based auto-detection for
   registry-visible changes (regroups, device moves), manual marking for
   controller-side changes (pacing, dedupe).
5. **HA surfacing:** yes; a `recommendations_open` sensor publishes through
   the already-granted discovery tile so controller-side automations can
   react.
6. **Detector priorities:** for the reference deployment the build order is
   pacing advisor → groupcast economics → redundant-command costing →
   reporting-configuration advisor → rebalancing advisor → retry hotspots.
   (The generic default ordering in §V2-5 stands for the product.)

**Additionally ratified:** a standing GUI principle; every V2 surface must
be understandable to someone with a cursory grasp of network engineering:
all granular data available, cogently presented, plain-language labels with
tooltips carrying the depth.
