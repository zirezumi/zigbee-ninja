import json
import time

from zigbee_ninja.attribution.chains import ChainTracker, payload_key_digests
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
    assert client.get("/api/attribution/conflicts").status_code == 401
    assert client.get("/api/attribution/noops").status_code == 401


def test_noop_verdict_is_stamped_end_to_end_through_ingest(client):
    """The detector must survive the real path: discovery, a command that
    cannot yet be judged, the device's own report, then the same command
    again. A unit test cannot catch a wiring mistake between _handle_message
    and the chains INSERT."""
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    clock = FakeClock(float(int(time.time() / 10) * 10))
    engine.chains = ChainTracker(resolve_members=engine._resolve_members, clock=clock)
    engine.on_message("z2m-test/bridge/info", json.dumps(INFO).encode())
    engine.on_message("z2m-test/bridge/devices", json.dumps(DEVICES).encode())

    engine.on_message("z2m-test/lamp/set", b'{"brightness": 100}')  # cold -> unknown
    clock.now += 0.3
    engine.on_message("z2m-test/lamp", b'{"brightness": 100, "state": "ON"}')
    clock.now += 0.3
    engine.on_message("z2m-test/lamp/set", b'{"brightness": 100}')  # -> noop
    clock.now += 0.3
    engine.on_message("z2m-test/lamp/set", b'{"brightness": 200}')  # -> changing

    # Chains persist only once finalized (window + lateness), and _expire runs
    # on intake, so advance past it and give the tracker one more message.
    clock.now += 20
    engine.on_message("z2m-test/other_sensor", b'{"temperature": 21}')

    body = client.get("/api/attribution/noops?seconds=600").json()
    assert body["counts"]["noop"] == 1
    assert body["counts"]["changing"] == 1
    assert body["counts"]["unknown"] == 1
    assert body["resolution_coverage"] == round(2 / 3, 4)
    assert body["noop_keys"][0] == {"key": "brightness", "noops": 1}
    # The echo table was actually fed: distinguishes "no no-ops" from
    # "the detector never saw anything".
    assert body["live"]["echoes"]["parses"] == 1


def test_noop_report_separates_unstamped_rows_from_undecidable_ones(client):
    """A row written before the detector existed is NULL, not 'unknown'.
    Folding them together would let old data dilute the coverage figure the
    whole report is meant to be read through."""
    client.post("/api/setup", json=SETUP)
    now = time.time()
    client.app.state.db.connect().execute(
        "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
        "echo_count, first_echo_ms, redundant, payload_digest, clock_skew_ms, payload_keys) "
        "VALUES ('z2m-1', 'lamp', 'set', ?, 'x', 10, 0, NULL, 0, 'd', 0, NULL)",
        (now,),
    )
    client.app.state.db.connect().commit()

    body = client.get("/api/attribution/noops?seconds=600").json()
    assert body["counts"] == {"unstamped": 1}
    # No stamped rows at all, so there is no coverage to report: explicitly
    # null rather than a misleading 0.0 or 1.0.
    assert body["resolution_coverage"] is None


def test_conflicts_endpoint_reports_a_disagreement(client):
    client.post("/api/setup", json=SETUP)
    now = time.time()
    writers = ((0.0, "automation: Lifecycle", 200), (0.3, "automation: Rendering", 120))
    for offset, who, bri in writers:
        client.app.state.db.connect().execute(
            "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
            "echo_count, first_echo_ms, redundant, payload_digest, clock_skew_ms, payload_keys) "
            "VALUES ('z2m-1', 'lamp', 'set', ?, ?, 10, 0, NULL, 0, 'd', 0, ?)",
            (now + offset, who, payload_key_digests(b'{"brightness": %d}' % bri)),
        )
    client.app.state.db.connect().commit()

    body = client.get("/api/attribution/conflicts?seconds=600&window=2").json()
    assert body["novel_cross_commander_pairs"] == 1
    assert body["conflicts"][0]["key"] == "brightness"
    assert body["conflicts"][0]["kind"] == "novel"
