# zigbee-ninja-probe (Z2M runtime extension)

The probe itself lives **inside the collector package** so the container can
deploy it over MQTT without extra assets:
[`collector/zigbee_ninja/probe_assets/zigbee-ninja-probe.js`](../../collector/zigbee_ninja/probe_assets/zigbee-ninja-probe.js)

A single-file, dependency-free JS extension (DESIGN.md §7.1), deployed and
removed entirely over MQTT (`bridge/request/extension/save` / `remove`) by the
tile manager. It hooks Zigbee2MQTT's event bus **defensively** — every hook
attaches through a capability check with a legacy fallback, and the heartbeat
self-reports the attached hook inventory, so spike S3 ("which hooks are stable
on this Z2M version?") is answered empirically by every deployment.

Emits batched compact events on `<base>/zigbee-ninja/probe/events` and a
heartbeat on `<base>/zigbee-ninja/probe/heartbeat`; payload sizes only, never
contents; fixed-size buffer with drop accounting; kill switch on
`<base>/zigbee-ninja/probe/set` (`{"enabled": false}`).

CI runs `node --check` over the probe; a proper unit harness against pinned Z2M
versions is the M3 follow-through once live deployments confirm the hook set.
