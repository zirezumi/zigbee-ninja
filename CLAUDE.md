# zigbee-ninja

Self-hosted, single-container observability service for Zigbee2MQTT systems:
measures per-coordinator throughput utilization/headroom and attributes traffic to
its causes. Python/FastAPI collector + React/TS GUI + embedded SQLite/Parquet
storage, shipped as one multi-arch image (HA add-on packaging is a fast-follow).

## The spec

**`docs/DESIGN.md` is canonical.** Read it before architectural work; section
numbers (§) referenced in code comments point there. If an implementation decision
deviates from the doc, update the doc in the same change — the doc must never lag
the code.

## Milestone state

M0 (scaffold: config store, auth shell, CI, container) — done.
M1 (broker onboarding, discovery via `+/bridge/info` + registries, 1s rate
tracking with 10s rollups, live fleet view over WebSocket) — done.
M2 (attribution v1: T0 command chains with group expansion + provoked/autonomous
classification, T0.5 Mosquitto `$SYS/broker/log` client correlation, redundant-
command detector, `chains` + `attribution_10s` tables, attribution explorer
view) — done.
M3 (T1: defensive single-file extension probe in
`collector/zigbee_ninja/probe_assets/` deployed/revoked over
`bridge/request/extension/save|remove` with transaction-correlated responses;
`tiles` table + TileManager lifecycle w/ heartbeat health/drift + revoke-all;
Footprint view; probe ingest w/ seq-gap accounting; T1 command→response latency
on the probe's own clock, surfaced on fleet cards; MQTT publish path with
`self` class accounting) — done. Spike S3 is answered EMPIRICALLY: the probe
heartbeat self-reports its attached hook inventory, shown on the Footprint page.
NOTHING M1–M3 is live-broker-soaked yet — the probe has never run inside a real
Z2M instance (CI only does `node --check`); first deploy must watch the
heartbeat hooks list + Z2M log.
M4 groundwork done: `decode/` now holds the ASH stream decoder (escaping,
CRC16-CCITT w/ standard check-value test, LFSR derandomization, cancel/
substitute, reTx accounting; written from UG101 — NOT from GPL bellows/zigpy),
the EZSP envelope parser (legacy↔extended switch driven by observed version
negotiation; core frame-name map pending S1 validation), a classic-pcap reader
with in-order TCP reassembly (retransmit dedupe + gap accounting), and the
spike-S1 command: `python -m zigbee_ninja.decode.pcap_cli capture.pcap --port
6638`. All synthetic-fixture tested end-to-end.
Remaining for **M4**: spike S1 (live golden capture on the reference host →
run the CLI → pin fixtures, validate frame-name map + deep parameter decode),
ninja-tap agent + collector WS ingest, T1/T2 fusion, spike S2 (counters).
Also M1 leftovers: commit `frontend/package-lock.json`, flip CI + Dockerfile
to `npm ci`. Deploy-to-LXC + live-broker soak pending (needs broker
credentials). Roadmap: README.md.

## Hard rules

- **License policy:** Apache-2.0; **no GPL/AGPL dependencies** in anything
  distributed (`bellows`, `zigpy`, `scapy` are explicitly banned — port from
  MIT zigbee-herdsman or Silabs UG100 instead). `tools/license_check.py` enforces
  in CI; run it after adding any dependency.
- **DCO:** commit with `-s` (Signed-off-by).
- **Public-repo hygiene:** this repo is public. Never commit the owner's
  infrastructure specifics (LAN IPs, hostnames, VMIDs, topology) — the reference
  deployment lives in the owner's private notes, not here. Test fixtures must be
  sanitized (IEEE addresses, network keys).
- **Product invariants** (DESIGN §2): probes passive + fail-open; every active
  capability permission-gated and revocable via the footprint model; zigbee-ninja's
  own traffic self-attributed; every metric provenance-tagged
  (measured/modeled/inferred); single-image architecture — no sidecar services.

## Dev commands

- `make venv` / `make api` (collector on :8686) / `make test` / `make lint` /
  `make licenses` / `make image`
- Frontend: `cd frontend && npm install && npm run dev` (proxies /api → :8686)
- Tests live in `collector/tests/`; run from `collector/` (`make test` handles it).

## Layout (DESIGN §17)

`collector/zigbee_ninja/{ingest,decode,attribution,capacity,calibration,store,api}`
— tier adapters, ASH/EZSP decode, chains/classes, airtime math, benchmark engine,
SQLite+Parquet, FastAPI. `frontend/` GUI. `probes/z2m-extension/` single-file JS
probe (M3). `agents/ninja-tap/` capture agent (M4). `deploy/` Dockerfile/compose.
`tests/fixtures/` golden pcaps + MQTT cassettes (sanitized).
