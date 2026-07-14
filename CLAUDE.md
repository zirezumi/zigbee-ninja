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
Next: **M1** — broker onboarding + discovery (`+/bridge/info` → instances,
adapter endpoints, channels; `bridge/devices|groups` registries) + live fleet
view + retention v0. Roadmap table: README.md. Spikes S1–S3 gate M3/M4 (DESIGN §19).

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
