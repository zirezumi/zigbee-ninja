# zigbee-ninja 🥷

Self-hosted observability for Zigbee networks managed by [Zigbee2MQTT](https://www.zigbee2mqtt.io/).

zigbee-ninja answers, with defensible numbers: **how much of each Zigbee coordinator's
throughput capacity is being used — and by what?** It runs continuously as a single
container with a built-in web GUI, attributes traffic to its causes (controller
commands, device reporting, housekeeping, retry overhead), models mesh airtime
amplification for group/broadcast traffic, and calibrates each coordinator's real
capacity knee with a guided benchmark.

**Status: pre-alpha — M1.** The architecture is fully specified in
[docs/DESIGN.md](docs/DESIGN.md). Broker onboarding, Z2M instance discovery, and
the live fleet view work; attribution, wire-tier capture, and capacity math are
still ahead. Not yet generally usable.

## Principles

- **Passive by default.** Observation never adds mesh traffic; active operations
  (topology pulls, calibration) are permission-gated, rate-limited, and revocable.
- **Consent per foothold.** Every probe deployment is an explicit grant in the GUI,
  listed on a footprint page with one-click removal. Probes fail open.
- **Confidence-tagged.** Every metric is labeled measured / modeled / inferred.
- **Generic core.** Everything is derived from discovery and live Z2M registries;
  no installation-specific logic.

## Observation tiers

| Tier | Source | Foothold |
|---|---|---|
| T0 | MQTT firehose | broker credentials only |
| T0.5 | Broker publish attribution (`$SYS/broker/log/#`) | broker config change |
| T1 | Z2M runtime extension (installed/removed over MQTT) | broker credentials only |
| T2 | Passive wire tap of the coordinator link (network adapters) | tiny host agent |
| T3 | RF sniffer | reserved, not V1 |

## Roadmap

| Milestone | Deliverable |
|---|---|
| M0 ✓ | Repo scaffold, CI, container skeleton, config store, auth shell |
| M1 ✓ | Broker onboarding + discovery + live fleet view |
| **M2 ← next** | Attribution v1 (command chains, taxonomy, client attribution) |
| M3 | Z2M extension probe + permission tiles + queue latency |
| M4 | Wire tap agent + ASH/EZSP decode + fusion |
| M5 | Airtime/capacity model + calibration wizard + headroom dashboards |
| M6 | Alerting + MQTT-discovery entities → **V1** |
| follow-ups | Home Assistant add-on packaging, ZHA support, what-if rebalancing advisor |

## Development

```sh
make venv     # create .venv and install the collector (editable, with dev deps)
make api      # run the collector on :8686
make web      # run the frontend dev server (proxies /api to :8686)
make test     # pytest
make lint     # ruff
make image    # build the container image
```

## License

[Apache-2.0](LICENSE). Contributions are accepted under the
[Developer Certificate of Origin](CONTRIBUTING.md) (`git commit -s`).
Dependency policy: no GPL/AGPL code in distributed artifacts — enforced by
`tools/license_check.py` in CI (rationale in [docs/DESIGN.md](docs/DESIGN.md) §16).

Zigbee® is a registered trademark of the Connectivity Standards Alliance.
This project is not affiliated with or endorsed by the CSA.
