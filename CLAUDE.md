# zigbee-ninja

Self-hosted, single-container observability service for Zigbee2MQTT systems:
measures per-coordinator throughput utilization/headroom and attributes traffic to
its causes. Python/FastAPI collector + React/TS GUI + embedded SQLite/Parquet
storage, shipped as one multi-arch image (HA add-on packaging is a fast-follow).

## The spec

**`docs/DESIGN.md` is canonical.** Read it before architectural work; section
numbers (§) referenced in code comments point there. If an implementation decision
deviates from the doc, update the doc in the same change: the doc must never lag
the code.

## Milestone state

M0 (scaffold: config store, auth shell, CI, container); done.
M1 (broker onboarding, discovery via `+/bridge/info` + registries, 1s rate
tracking with 10s rollups, live fleet view over WebSocket): done.
M2 (attribution v1: T0 command chains with group expansion + provoked/autonomous
classification, T0.5 Mosquitto `$SYS/broker/log` client correlation, redundant-
command detector, `chains` + `attribution_10s` tables, attribution explorer
view): done.
M3 (T1: defensive single-file extension probe in
`collector/zigbee_ninja/probe_assets/` deployed/revoked over
`bridge/request/extension/save|remove` with transaction-correlated responses;
`tiles` table + TileManager lifecycle w/ heartbeat health/drift + revoke-all;
Footprint view; probe ingest w/ seq-gap accounting; T1 command→response latency
on the probe's own clock, surfaced on fleet cards; MQTT publish path with
`self` class accounting): done. Spike S3 is answered EMPIRICALLY: the probe
heartbeat self-reports its attached hook inventory, shown on the Footprint page.
NOTHING M1–M3 is live-broker-soaked yet: the probe has never run inside a real
Z2M instance (CI only does `node --check`); first deploy must watch the
heartbeat hooks list + Z2M log.
M4 (wire tier): `decode/` ASH stream decoder (UG101 clean-room, NOT from GPL
bellows/zigpy), EZSP envelope parser, streaming pcap reader w/ TCP reassembly:
all validated against live coordinator captures (spike S1: 0 CRC / 0 parse
errors on real silicon). `ninja-tap` capture agent + `/api/ws/tap`
token-authenticated streaming ingest are LIVE on the reference deployment (one
host agent covers every coordinator flow; stable). Deep EZSP parameter decode
(`decode/ezsp_params.py`) pinned EMPIRICALLY against live EZSP v14-era captures
(32-bit sl_status, 16-bit tags, rx-packet-info); every parser self-checks frame
length arithmetic and degrades to a `layout_mismatch` counter, never silently
wrong numbers. Spike S2 is resolved passively: Z2M's own readAndClearCounters responses are
harvested off the wire and labeled (`decode/counters.py`, provenance inferred
until live validation); wire-latency 10 s windows persist to `latency_10s`
(`/api/latency`) as the latency-vs-load axis for continuous knee validation.
**M4 complete: T1/T2 fusion built**: incoming frames fuse on (instance,
sender nwk via the registry name→nwk join, ZCL transaction sequence) in
`ingest/fusion.py`: the wire side reads the sequence from the ZCL header
(metadata only), probe v0.4 emits it per deviceMessage (appended fields, old
collectors/probes stay compatible); matched/wire-only/probe-only rolling
counters + probe↔pcap clock-offset EWMA per instance, on the fleet WS,
/api/tap, and a Wiretap "Frame fusion" row. Deployed probes predate v0.4
until the owner updates them from Footprint (drift chip → Deploy: consent
per foothold); fusion honestly reports "awaiting probe v0.4" meanwhile.
**M5 (started)**: per-frame airtime model (`capacity/airtime.py`, DESIGN §10)
with PSDU reconstruction + mesh amplification over the discovered router
census; per-instance airtime buckets (tx_unicast / tx_groupcast / rx / rx_mesh)
with 1s live views, `airtime_10s` rollups, and `/api/airtime`; wire-tier
latency SLI (sendUnicast→messageSentHandler tag pairing on pcap timestamps):
the authoritative replacement for the T1 command→echo proxy: plus delivery
statuses, route-record/route-error mesh-health counters, and per-frame LQI/RSSI
EWMAs on the fleet path. HA-token per-automation attribution (§7.4) is LIVE
(100% publish naming on the reference deployment). Topology snapshots are LIVE:
grant-gated networkmap pulls (15-min rate limit, one scan at a time),
`topology_snapshots` storage + summaries + Topology view; per-query failure
detail distinguishes firmware ZDO omissions from unreachable nodes.
**Calibration wizard (§11) is BUILT: unicast stage**: per-run authorization
(dry-run preview mints a single-use TTL'd token; no grants persist),
closed-loop /get ramp with outstanding bound, knee detection (timeout ratio,
delivery failures, p95 RTT breach vs step-1 baseline, driver saturation =
pipeline ceiling), watchdog aborts (uninvolved-device offline, Z2M error-log
spike, total silence, manual, hard caps), cooldown, `calibrations` table
(migration 8) with per-step curves, self-attributed traffic end to end,
candidates ranking from topology LQI − bindings/groups, and the Calibration
GUI view (preview/authorize/live-ramp/history + recalibrate-on-drift chip).
All five reference coordinators are calibrated (owner-authorized): every run
completed cleanly via the saturation rule with wire RTT flat throughout:
single-target runs measure the Zigbee2MQTT per-device request-queue ceiling
(denominator 3, ~1/RTT) and bound the NCP knee from below; driving the NCP
itself needs a multi-target spread (future refinement). **Bulk calibration**
is live: one dry-run preview enumerates a batch (auto-picked or pinned
targets), one single-use token authorizes exactly that list, runs execute
sequentially with the full cooldown, aborts stop the remainder, vanished
targets leave `skipped` history rows. **Headroom dashboards close the M5
GUI**: `capacity/headroom.py` + GET /api/headroom report the three §10
denominators side by side, steady/burst headroom vs the knee (10 s
granularity), and the latency-vs-load knee-validation scatter: with
benchmark windows excluded from every aggregate (§11.5); the Headroom view
(uPlot) draws the scatter with the knee overlay and Fleet cards carry a knee
line. **M5 tail complete**: avg_tx is measured passively from the
coordinator's own broadcast TX counters (the groupcast stage is superseded:
see §10), and **spread mode** round-robins reads across the top routers to
probe the NCP/global-pipeline knee (denominator 2; knees stored per mode,
headroom prefers the spread measurement). Reference fleet fully calibrated in
both modes; the five instances show a uniform ~2× spread-over-single knee
with the wire RTT inflating as the global ceiling approaches: the shared
Z2M/EZSP stack, not the radio, is the binding constraint there.
**M5 is done.**
**M6 (started)**: alerting engine (§14); migration 9 `alert_rules`/`alert_events`,
evaluator ticking with the engine's 10 s flush loop (sustain-before-open,
clear-on-hysteresis with a 60 s floor, freeze-on-missing-data, counter metrics
as per-tick deltas with restart rebaselining), open events survive restarts,
built-ins seed exactly once (self-health enabled / capacity disabled: user
deletions durable), rule CRUD + active/history API (`/api/alerts*`), active
alerts on the fleet WS. Alerts GUI view live (rule editor from the metric
catalog, event history, fleet-card chips + service-wide banner). MQTT
discovery publisher (§14) as a per-instance GRANT tile (`mqtt_discovery`):
retained HA discovery configs under the instance's announced prefix +
per-metric state topics + problem binary_sensor on a 45 s loop, expire_after
instead of availability/LWT, revoke deletes every claimed retained topic
(bookkept in settings; loop sweep finishes cleanup if the broker was down).
Secrets-at-rest (§15) done: Fernet key `secret.key` (0600) in the data
volume, `enc:`-marked ciphertext, idempotent startup upgrade of plaintext
broker password / HA token, undecryptable → unconfigured (re-enter in GUI);
`cryptography` dependency (Apache/BSD, license-gate clean). Settings view +
retention knobs (§12) done: settings-backed rollup-days / chain-hours /
topology-snapshots-kept (clamped server-side), client labels annotating the
Attribution explorer, tap-token reveal. **avg_tx live fixes**: Z2M's ember
watchdog polls counters HOURLY (setInterval 3600000: live-verified in
herdsman source + observed gaps 3599–3615 s), so the old 60–3600 s window
guard rejected nearly every sample; ceiling now 7500 s (one fused window).
And `mac_tx_broadcast` provably includes relayed foreign NWK broadcasts
(live windows exceed the passive-ack ×3 maximum even at full retry), so
residuals > 3 are discarded as relay-contaminated (counted, shown in Wire
view) instead of clamping to a fake 3.0: on busy meshes avg_tx honestly
stays modeled 1.3.
**M6 DEPLOYED + LIVE-VALIDATED**: migration + seeded rules on the persisted
volume; secrets upgraded in place with broker/HA reconnecting through the
decrypt path; alert lifecycle proven end to end on live infrastructure
(agent stopped → open after sustain → GUI banner/chips → restart → clear on
hysteresis → history); both avg_tx fixes confirmed on live hourly windows
(3595–3604 s accepted by the widened guard; all residuals 3.16–3.50 →
discarded as relay-contaminated with visible accounting).
**Burst inspector + §12 raw-event store DONE (owner ruled it V1 scope)**:
bounded buffer → hot DuckDB table on the 10 s flush → hourly ZSTD Parquet
segments, horizon+quota retention (settings-backed), T0 MQTT + T2 EZSP frame
capture, /api/burst/{timeline,events,chains}, zoomable uPlot timeline view
(drag re-queries finer buckets to 10 ms; ≤120 s windows show raw rows) with
the window's chains as a micro-gantt-lite table: live-validated (both
sources captured, HA-attributed chains render). Discovery tiles granted by
the owner on all five instances: entities live in HA. **V1 is
feature-complete; cosign image signing (§15) at first release.**
**Post-V1 GUI comprehensibility pass (owner review round, `7ae9d87`)**:
views are hash-routed (refresh/back preserve navigation; broker Reconfigure
is a cancellable route); Fleet is a list of full-width rows with axis-labeled
live rate charts (uPlot) and the broker host:port in the banner; coverage
chips spell out the tiers instead of T0/T1/T2; every fleet fact and most
Wiretap/Benchmark/Headroom metrics carry plain-language tooltips; the knee is
presented as "capacity limit" in ALL user-facing text (metric ids stay
knee_*: DESIGN §13 terminology note); "Wire tap" → **Wiretap**; "Burst
inspector" → nav label **Benchmark**; Footprint grant-tile rows no longer
repeat the extension probe's hooks/drops; HA discovery sensor renamed
"Capacity utilization" (unique_id stable: verified renamed in live HA).
**Post-V1 §8 tail (same day)**: (1) **retry-factor hook**; unicast TX airtime
× (1 + retry_rate), measured per flow from mac_tx_unicast_retry/_success on
every clearing counter read (self-contained ratio, no window length needed;
EWMA 0.2, ≥50-success floor, clamped at macMaxFrameRetries 3), surfaced as the
Wiretap "Unicast retries" row; live samples land with Z2M's hourly counter
polls. (2) **Topology force-directed graph**: GET /api/topology/graph reduces
the stored raw map server-side (one edge per pair keeping the worse
direction's LQI; `routes_via` counts ACTIVE routing-table rows per next-hop
node = observed relay load), rendered with d3-force (ISC, §16 named
inclusion) settled synchronously: weak links dashed, routers sized by
degree + routed paths; live-verified 5/5 instance graphs. (3) **Release
pipeline**: tag-triggered `.github/workflows/release.yml` (gates re-run →
immutable vX.Y.Z multi-arch image → cosign keyless signing → GitHub release);
process + verification command in docs/RELEASING.md; whether releases also
move `:latest` is an open owner decision (CI main-push semantics unchanged).
(4) **docs/V2_PROPOSAL.md** drafted (optimization loop: cost ledger, change
journal, budgets/regression alerts, counterfactual-replayed recommendations,
migration manifest, applied-change verification): iterate with the owner
before building. (5) **T1/T2 fusion built** (see the M4 entry above); owner updated all five
probes to v0.4 (2026-07-16, via the new one-click in-place Update button on
drifted Footprint tiles): and after diagnosis, **fusion is live-validated
fleet-wide: 89–100% of wire frames match a probe event, probe-only is ZERO
(no capture gaps), clock offsets ~0.1 ms**. The initial ~⅓ match rate was a
transport bug the diagnostics chain isolated (per-sender `top_unmatched` →
seq-delta histogram dominated by delta 0 = same key arriving >5 s apart):
the ninja-tap agent pumped tcpdump output with `read(16384)`, which blocks
until a full block accumulates: quiet flows' frames were delayed tens of
seconds. Fixed with `read1()` (agent redeployed on the capture host); every
pcap-timestamp consumer was always correct, but live-view freshness on
quiet flows improved too. The surviving wire-only residue (7–11%, zero on
the quietest instance) is the designed signal: frames Z2M consumes without
emitting a device event (default responses to commands, interview traffic).
**V2 RATIFIED 2026-07-16 (owner)**: all §V2-10 questions resolved in
docs/V2_PROPOSAL.md; **V2.M1 (cost ledger + change journal + attribution
cost columns) is green-lit**: reference-deployment detector order: pacing →
groupcast economics → redundancy costing → reporting advisor → rebalancing →
retry hotspots. Image tags ratified: main → :edge, releases → :latest
(docs/RELEASING.md); the v0.1.0 tag itself remains the owner's call.
Remaining: forensics leads (owner-interest-gated: incoming trailing byte
0x02/0x04, EZSP 0x0024, rare tag mispairs).
**V2.M1 BUILT (cost ledger + change journal + GUI)**: migration 10
`ledger_daily`/`ledger_device_daily` (µs, provenance + pricing params per
row, 365 d) filled at the 10 s flush: every finalized chain priced
(group-vs-device via the registry, router-census amplification, measured
per-flow avg_tx/retry_rate with §10 defaults recorded when absent),
autonomous state publishes priced per device, zigbee-ninja's own mesh
commands (benchmark reads) priced under the self commander TX-only.
Migration 11 `journal` (90 d): registry diffs on devices/groups/info
(added/removed/renamed/rejoined, membership, version/channel/firmware),
first-sight-is-baseline so restarts and retained republishes journal
nothing, cross-instance move annotated `moved_from`. GET /api/ledger
(windows round out to whole UTC days; rates divide by elapsed wall clock
since the earliest returned day, response states both) + GET /api/journal.
GUI: Attribution cost columns + Top spenders (headline % of budget, µs/s
alongside), Fleet Recent changes panel. **V2.M1 DEPLOYED + LIVE-VALIDATED
2026-07-16**: commanders and devices priced live within minutes (Rendering
automations named per instance, presence dimmers dominating autonomous,
group-amplification arithmetic hand-checked exact); two defects the first
live data exposed were fixed same-day (group state topics excluded from
device costing: Z2M-synthetic, not mesh frames; rate denominators bounded
by a seed-once `ledger_since` so fresh deployments divide by recorded
time). Journal live-validated for baseline suppression (two restarts with
full retained republishes journaled nothing); a positive-path live entry
awaits the next natural registry change. Rates start `modeled/default`
after each restart and flip to measured as hourly counter windows land.
Next: V2.M2 (cost baselines + regression alerts + Top spenders trend,
riding the alert engine).
Roadmap: README.md.

## Hard rules

- **License policy:** Apache-2.0; **no GPL/AGPL dependencies** in anything
  distributed (`bellows`, `zigpy`, `scapy` are explicitly banned: port from
  MIT zigbee-herdsman or Silabs UG100 instead). `tools/license_check.py` enforces
  in CI; run it after adding any dependency.
- **DCO:** commit with `-s` (Signed-off-by).
- **Public-repo hygiene:** this repo is public. Never commit the owner's
  infrastructure specifics (LAN IPs, hostnames, VMIDs, topology): the reference
  deployment lives in the owner's private notes, not here. Test fixtures must be
  sanitized (IEEE addresses, network keys).
- **Product invariants** (DESIGN §2): probes passive + fail-open; every active
  capability permission-gated and revocable via the footprint model; zigbee-ninja's
  own traffic self-attributed; every metric provenance-tagged
  (measured/modeled/inferred); single-image architecture: no sidecar services.
- **GUI principle (owner mandate, 2026-07-16):** every view must be
  understandable to someone with a cursory grasp of network engineering: all
  granular data available, cogently presented; plain-language labels with
  tooltips carrying the depth; no design-doc jargon in UI text.
- **No em dashes in prose (owner policy, 2026-07-16):** they read as an
  AI-prose tell. Use colons, semicolons, commas, or parentheses instead,
  in GUI text, docs, comments, and commit messages alike. The character is
  allowed only as the empty-value glyph in UI data cells and as a table
  placeholder symbol.

## Dev commands

- `make venv` / `make api` (collector on :8686) / `make test` / `make lint` /
  `make licenses` / `make image`
- Frontend: `cd frontend && npm install && npm run dev` (proxies /api → :8686)
- Tests live in `collector/tests/`; run from `collector/` (`make test` handles it).

## Layout (DESIGN §17)

`collector/zigbee_ninja/{ingest,decode,attribution,capacity,calibration,store,api}`
holds tier adapters, ASH/EZSP decode, chains/classes, airtime math, benchmark engine,
SQLite+Parquet, FastAPI. `frontend/` GUI. `probes/z2m-extension/` single-file JS
probe (M3). `agents/ninja-tap/` capture agent (M4). `deploy/` Dockerfile/compose.
`tests/fixtures/` golden pcaps + MQTT cassettes (sanitized).
