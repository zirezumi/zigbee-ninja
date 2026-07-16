import asyncio
import json
import time

from zigbee_ninja.attribution.chains import ChainTracker
from zigbee_ninja.capacity import airtime, ledger

SETUP = {"username": "admin", "password": "correct-horse"}

INFO = {
    "version": "2.3.0",
    "network": {"channel": 15},
    "config": {"serial": {"port": "tcp://x:1"}},
}
DEVICES = [
    {
        "ieee_address": "0x02",
        "friendly_name": "lamp",
        "type": "Router",
        "power_source": "Mains",
        "definition": {"vendor": "V", "model": "M"},
    },
    {
        "ieee_address": "0x03",
        "friendly_name": "strip",
        "type": "Router",
        "power_source": "Mains",
        "definition": {"vendor": "V", "model": "M"},
    },
    {
        "ieee_address": "0x04",
        "friendly_name": "motion",
        "type": "EndDevice",
        "power_source": "Battery",
        "definition": {"vendor": "V", "model": "M"},
    },
]
GROUPS = [
    {
        "id": 7,
        "friendly_name": "room_group",
        "members": [{"ieee_address": "0x02"}, {"ieee_address": "0x03"}],
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


def _prepare_engine(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    clock = FakeClock(float(int(time.time() / 10) * 10))
    engine.chains = ChainTracker(resolve_members=engine._resolve_members, clock=clock)
    engine.on_message("z2m-test/bridge/info", json.dumps(INFO).encode())
    engine.on_message("z2m-test/bridge/devices", json.dumps(DEVICES).encode())
    engine.on_message("z2m-test/bridge/groups", json.dumps(GROUPS).encode())
    return engine, clock


def _ledger_rows(client):
    conn = client.app.state.db.connect()
    return {
        row["commander"]: dict(row)
        for row in conn.execute(
            "SELECT commander, SUM(chains) AS chains, SUM(tx_us) AS tx_us, "
            "SUM(rx_us) AS rx_us, MAX(provenance) AS provenance, MAX(params) AS params "
            "FROM ledger_daily GROUP BY commander"
        )
    }


def test_flush_prices_finalized_chains_into_daily_ledger(client):
    engine, clock = _prepare_engine(client)

    # Attributed unicast set with one echo; after its window closes, a group
    # set claiming both member echoes; then an autonomous report.
    engine.on_message("$SYS/broker/log/D", LOG_LINE)
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    clock.now += 0.3
    engine.on_message("z2m-test/lamp", b'{"state":"ON"}')
    clock.now += 2.0
    engine.on_message("z2m-test/room_group/set", b'{"brightness":128}')
    clock.now += 0.3
    engine.on_message("z2m-test/lamp", b'{"brightness":128}')
    engine.on_message("z2m-test/strip", b'{"brightness":128}')
    engine.on_message("z2m-test/motion", b'{"occupancy":true}')
    clock.now += 20
    engine.flush_rollups()

    rows = _ledger_rows(client)
    named = rows["ha-core"]
    assert named["chains"] == 1
    assert named["tx_us"] == airtime.unicast_airtime_us(ledger.ZCL_SET_BYTES)
    assert named["rx_us"] == ledger.autonomous_publish_cost_us()

    unattributed = rows[ledger.UNATTRIBUTED]
    assert unattributed["chains"] == 1
    assert unattributed["tx_us"] == airtime.groupcast_airtime_us(
        ledger.ZCL_SET_BYTES, 2, avg_tx=airtime.DEFAULT_AVG_TX
    )
    assert unattributed["rx_us"] == 2 * ledger.autonomous_publish_cost_us()

    params = json.loads(named["params"])
    assert params["n_routers"] == 2
    assert params["avg_tx_measured"] is False
    assert params["retry_rate_measured"] is False
    assert named["provenance"].startswith("inferred")

    device_rows = {
        row["device"]: dict(row)
        for row in client.app.state.db.connect().execute(
            "SELECT device, publishes, autonomous_us, provenance FROM ledger_device_daily"
        )
    }
    assert set(device_rows) == {"motion"}
    motion = device_rows["motion"]
    assert motion["publishes"] == 1
    assert motion["autonomous_us"] == ledger.autonomous_publish_cost_us()
    assert motion["provenance"].startswith("modeled")


def test_measured_flow_params_feed_chain_prices(client):
    engine, clock = _prepare_engine(client)
    engine.tap.pricing_params = lambda instance: (2.0, 0.05)

    engine.on_message("z2m-test/room_group/set", b'{"state":"ON"}')
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    clock.now += 20
    engine.flush_rollups()

    row = _ledger_rows(client)[ledger.UNATTRIBUTED]
    assert row["chains"] == 2
    assert row["tx_us"] == airtime.groupcast_airtime_us(
        ledger.ZCL_SET_BYTES, 2, avg_tx=2.0
    ) + airtime.unicast_airtime_us(ledger.ZCL_SET_BYTES, retry_rate=0.05)
    params = json.loads(row["params"])
    assert params["avg_tx"] == 2.0
    assert params["avg_tx_measured"] is True
    assert params["retry_rate"] == 0.05
    assert params["retry_rate_measured"] is True


def test_self_mesh_commands_priced_under_self_commander(client):
    engine, _clock = _prepare_engine(client)

    class StubIngest:
        async def publish(self, topic, payload, retain=False):
            pass

    engine._ingest = StubIngest()
    asyncio.run(engine.publish("z2m-test/lamp/get", '{"state":""}'))
    asyncio.run(engine.publish("z2m-test/lamp/get", '{"state":""}'))
    # Bridge requests are not mesh commands and must not reach the ledger.
    asyncio.run(engine.publish("z2m-test/bridge/request/networkmap", "{}"))
    engine.flush_rollups()

    rows = _ledger_rows(client)
    assert set(rows) == {ledger.SELF_COMMANDER}
    row = rows[ledger.SELF_COMMANDER]
    assert row["chains"] == 2
    assert row["tx_us"] == 2 * airtime.unicast_airtime_us(ledger.ZCL_GET_BYTES)
    assert row["rx_us"] == 0


def test_group_state_topics_are_not_priced_as_device_reports(client):
    engine, clock = _prepare_engine(client)
    # No open chains: both publishes classify autonomous, but the group topic
    # is Zigbee2MQTT's synthetic optimistic state, not a mesh frame.
    engine.on_message("z2m-test/room_group", b'{"state":"ON"}')
    engine.on_message("z2m-test/motion", b'{"occupancy":true}')
    clock.now += 20
    engine.flush_rollups()

    device_rows = {
        row["device"]
        for row in client.app.state.db.connect().execute(
            "SELECT device FROM ledger_device_daily"
        )
    }
    assert device_rows == {"motion"}


def test_ledger_api_reports_costs_rates_and_rankings(client):
    engine, clock = _prepare_engine(client)
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    clock.now += 0.3
    engine.on_message("z2m-test/lamp", b'{"state":"ON"}')
    engine.on_message("z2m-test/motion", b'{"occupancy":true}')
    clock.now += 20

    view = client.get("/api/ledger?seconds=86400").json()
    assert view["days"] and view["effective_seconds"] > 0
    # Rates divide by recorded time (seeded at engine start moments ago),
    # never by day-range hours the ledger did not exist for.
    assert view["recording_since"] is not None
    assert view["effective_seconds"] < 120
    assert view["commander_count"] == 1
    row = view["commanders"][0]
    assert row["commander"] == ledger.UNATTRIBUTED
    assert row["total_us"] == row["tx_us"] + row["rx_us"]
    assert row["us_per_s"] > 0
    assert row["pct_of_budget"] >= 0
    assert row["params"]["n_routers"] == 2
    assert row["provenance"].startswith("inferred")

    device = view["devices"][0]
    assert device["device"] == "motion"
    assert device["publishes"] == 1
    assert device["autonomous_us"] == ledger.autonomous_publish_cost_us()

    totals = view["totals"]
    assert totals["total_us"] == totals["tx_us"] + totals["rx_us"] + totals["autonomous_us"]
    assert totals["chains"] == 1


def test_journal_api_returns_parsed_entries(client):
    engine, _clock = _prepare_engine(client)
    devices = list(DEVICES) + [
        {
            "ieee_address": "0x05",
            "friendly_name": "new_plug",
            "type": "Router",
            "power_source": "Mains",
            "definition": {"vendor": "V", "model": "M"},
        }
    ]
    engine.on_message("z2m-test/bridge/devices", json.dumps(devices).encode())

    view = client.get("/api/journal?seconds=3600").json()
    entries = view["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "device_added"
    assert entries[0]["subject"] == "new_plug"
    assert entries[0]["detail"]["ieee"] == "0x05"


def test_ledger_and_journal_require_auth(client):
    assert client.get("/api/ledger").status_code == 401
    assert client.get("/api/journal").status_code == 401


def test_group_chain_uses_amplification_and_measured_avg_tx():
    price = ledger.price_chain(
        verb="set", group_target=True, n_routers=20, echo_count=15, avg_tx=2.1
    )
    assert price.tx_us == airtime.groupcast_airtime_us(
        ledger.ZCL_SET_BYTES, 20, avg_tx=2.1
    )
    assert price.rx_us == 15 * ledger.autonomous_publish_cost_us()
    assert price.total_us == price.tx_us + price.rx_us
    assert price.provenance.startswith("inferred")
    assert price.params["avg_tx"] == 2.1
    assert price.params["retry_rate"] is None


def test_device_chain_uses_unicast_and_retry_rate():
    price = ledger.price_chain(
        verb="set", group_target=False, n_routers=20, echo_count=1, retry_rate=0.08
    )
    assert price.tx_us == airtime.unicast_airtime_us(ledger.ZCL_SET_BYTES, retry_rate=0.08)
    assert price.params["n_routers"] == 0
    assert price.params["retry_rate"] == 0.08


def test_get_prices_smaller_than_set_and_defaults_apply():
    get_price = ledger.price_chain(
        verb="get", group_target=False, n_routers=0, echo_count=0
    )
    set_price = ledger.price_chain(
        verb="set", group_target=False, n_routers=0, echo_count=0
    )
    assert get_price.tx_us < set_price.tx_us
    assert get_price.rx_us == 0
    assert get_price.params["retry_rate"] == 0.0
