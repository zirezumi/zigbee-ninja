# ninja-tap (wire-tap capture agent)

Lands in **M4** (DESIGN.md §7.2). Dumb agent, smart collector: shells out to
`tcpdump` with a BPF filter for the coordinator TCP flows discovery found, and
streams raw filtered pcap to the collector over an outbound, token-authenticated
WebSocket. No Zigbee knowledge in the agent: TCP reassembly and ASH/EZSP decode
happen collector-side (§7.3).

Ships as a reviewable one-liner installer (systemd unit, CPU/memory caps,
capture-capability-only privileges), with `ninja-tap uninstall` and GUI-driven
self-removal. Size-capped ring buffer + drop accounting when the collector is
unreachable.
