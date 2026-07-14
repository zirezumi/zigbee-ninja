import json
import time

from zigbee_ninja.attribution.chains import ChainTracker
from zigbee_ninja.ingest.rates import RateTracker

SETUP = {"username": "admin", "password": "correct-horse"}

INFO = {"version": "2.3.0", "network": {"channel": 15}, "config": {"serial": {"port": "tcp://x:1"}}}
DEVICES = [
    {
        "ieee_address": "0x02",
        "friendly_name": "lamp",
        "type": "Router",
        "power_source": "Mains",
        "definition": {"vendor": "V", "model": "M"},
    }
]

LOG_LINE = (
    b"1720000000: Received PUBLISH from ha-core "
    b"(d0, q0, r0, m0, 'z2m-test/lamp/set', ... (15 bytes))"
)


class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_end_to_end_attribution_flow(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine

    # Re-anchor trackers on a controllable clock near real time so rollup
    # timestamps land inside the query window.
    clock = FakeClock(float(int(time.time() / 10) * 10))
    engine.rates = RateTracker(clock=clock)
    engine.class_rates = RateTracker(clock=clock)
    engine.chains = ChainTracker(resolve_members=engine._resolve_members, clock=clock)

    # Discovery, then: attributed command -> echo (provoked) -> autonomous report.
    engine.on_message("z2m-test/bridge/info", json.dumps(INFO).encode())
    engine.on_message("z2m-test/bridge/devices", json.dumps(DEVICES).encode())
    engine.on_message("$SYS/broker/log/D", LOG_LINE)
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    clock.now += 0.3
    engine.on_message("z2m-test/lamp", b'{"state":"ON"}')
    clock.now += 0.3
    engine.on_message("z2m-test/other_sensor", b'{"temperature":21}')

    # Redundant pair.
    engine.on_message("z2m-test/lamp/set", b'{"state":"OFF"}')
    clock.now += 1.0
    engine.on_message("z2m-test/lamp/set", b'{"state":"OFF"}')

    clock.now += 20  # everything finalizes and rolls up

    summary = client.get("/api/attribution/summary?seconds=3600").json()
    classes = summary["classes"]["z2m-test"]
    assert classes["commanded"] == 3
    assert classes["provoked"] == 1
    assert classes["autonomous"] == 1

    lamp_rows = [t for t in summary["top_targets"] if t["target"] == "lamp"]
    assert lamp_rows and lamp_rows[0]["commands"] == 3
    assert lamp_rows[0]["redundant"] == 1

    clients = {c["client"]: c["commands"] for c in summary["top_clients"]}
    assert clients.get("ha-core", 0) >= 1  # the log-correlated command

    assert summary["totals"]["chains"] == 3
    assert summary["totals"]["redundant"] == 1

    redundant = client.get("/api/attribution/redundant?seconds=3600").json()["redundant"]
    assert redundant and redundant[0]["target"] == "lamp"


def test_attribution_endpoints_require_auth(client):
    assert client.get("/api/attribution/summary").status_code == 401
    assert client.get("/api/attribution/redundant").status_code == 401
