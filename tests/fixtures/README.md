# Cross-cutting test fixtures

Golden fixtures shared across components:

- **Golden pcaps** (spike S1, M4): captured EZSP-over-TCP coordinator flows that
  pin the ASH/EZSP decoder. Must be sanitized before commit — IEEE addresses
  anonymized, no network keys, no LAN addressing.
- **MQTT cassettes** (M1+): recorded `bridge/info` / `bridge/devices` /
  `bridge/groups` payloads for discovery and registry tests, likewise sanitized.

Nothing in this tree may identify a real installation (CLAUDE.md hygiene rules).
