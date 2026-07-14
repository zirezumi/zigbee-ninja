# zigbee-ninja-probe (Z2M runtime extension)

Lands in **M3** (DESIGN.md §7.1). A single-file, dependency-free JS extension,
deployed and removed entirely over MQTT (`bridge/request/extension/save` /
`remove`). Hooks Zigbee2MQTT's event bus and emits batched frame telemetry on
`<base>/zigbee-ninja/probe/{events,stats,heartbeat}`.

Constraints (spec §7.1): payload sizes only (no contents) unless deep-capture is
toggled; fixed-size buffer with drop accounting; kill-switch topic; version stamp
+ schema handshake with the collector. Opening spike **S3**: enumerate the stable
hook inventory across Z2M 2.x.
