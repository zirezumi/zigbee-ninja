# Contributing to zigbee-ninja

## Ground rules

- **The design document is the spec.** [docs/DESIGN.md](docs/DESIGN.md) is canonical;
  changes that alter the architecture must update it in the same PR.
- **License policy (hard rule):** no GPL or AGPL code in distributed artifacts —
  neither vendored nor as a dependency. Named exclusions: `bellows`, `zigpy`,
  `scapy`. CI enforces this via `tools/license_check.py`. Protocol-decoding logic
  is ported from MIT-licensed zigbee-herdsman or written from vendor specifications.
- **Probes stay passive and fail-open.** Nothing that ships may sit in a required
  datapath or transmit on the mesh without an explicit, revocable user grant.

## Developer Certificate of Origin

Contributions require a DCO sign-off — add `-s` to your commits:

```sh
git commit -s -m "your change"
```

This appends a `Signed-off-by:` trailer certifying you have the right to submit
the work under the project license (see <https://developercertificate.org/>).

## Development setup

```sh
make venv                 # python venv + collector in editable mode
make test && make lint    # what CI runs
cd frontend && npm install && npm run dev   # GUI dev server (proxies /api)
```

Python ≥ 3.10 (container runs 3.12), Node 22 for the frontend.

## Pull requests

- Keep changes scoped to one concern; include tests for behavior changes.
- `make test`, `make lint`, and `python tools/license_check.py` must pass.
- New metrics must carry a provenance tag (measured / modeled / inferred).
