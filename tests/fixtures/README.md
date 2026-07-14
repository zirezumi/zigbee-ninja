# Cross-cutting test fixtures

Golden fixtures shared across components:

- **Golden pcaps** (spike S1, M4): captured EZSP-over-TCP coordinator flows that
  pin the ASH/EZSP decoder. Must be sanitized before commit — IEEE addresses
  anonymized, no network keys, no LAN addressing. Raw captures live under
  `captures/` (gitignored) and never enter the public repo.

  **S1 status: validated.** A 60 s live capture of five Ember (SLZB-06MG24)
  coordinator flows decoded with **zero ASH CRC errors and zero EZSP parse
  errors**. It confirmed the clean-room ASH layer against real silicon and
  drove one decoder fix: a passive mid-stream capture never observes the EZSP
  version handshake, so the parser now defaults to the extended (v8+) header.
  Frame names (`incomingMessageHandler`, `sendUnicast`, `messageSentHandler`)
  decode correctly; frame id `0x0059` is a known-unlabeled callback pending
  identification.
- **MQTT cassettes** (M1+): recorded `bridge/info` / `bridge/devices` /
  `bridge/groups` payloads for discovery and registry tests, likewise sanitized.

Nothing in this tree may identify a real installation (CLAUDE.md hygiene rules).
