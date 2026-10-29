"""External command reports (DESIGN §9.1): a frame an extension sends from inside
Zigbee2MQTT, reported on `<base>/zigbee-ninja/ext/command` before it goes out, is
judged and chained like a `/set`; its client comes from the HA publish of the
message that caused it; non-entity topics are not device state."""

import hashlib
import json
import time

from zigbee_ninja.attribution.chains import correlation_digest
from zigbee_ninja.attribution.noop import VERDICT_CHANGING, VERDICT_NOOP, VERDICT_UNKNOWN
from zigbee_ninja.ingest.extreport import (
    CAUSE_MAX_ENTRIES,
    CAUSE_TTL_SECONDS,
    CauseRing,
    parse_report,
    raw_digest,
)
from zigbee_ninja.ingest.registry import Registry

REPORT_TOPIC = "z2m-test/zigbee-ninja/ext/command"
CAUSE_TOPIC = "z2m-test/someext/op"


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _engine(client):
    import test_registry as fixtures

    client.post("/api/setup", json={"username": "admin", "password": "correct-horse"})
    engine = client.app.state.engine
    engine.registry.handle("z2m-test/bridge/info", b'{"version": "2.3.0"}')
    engine.registry.handle("z2m-test/bridge/devices", json.dumps(fixtures.BOUND_DEVICES).encode())
    engine.registry.handle("z2m-test/bridge/groups", json.dumps(fixtures.BOUND_GROUPS).encode())
    return engine


def report(target, payload, cause: bytes | None = None, reporter="some-ext", **extra) -> bytes:
    body = {"v": 1, "reporter": reporter, "version": "1.0", "target": target, "payload": payload,
            "t": 1.0, "ref": "op-1", **extra}
    if cause is not None:
        body["cause"] = {"topic": CAUSE_TOPIC, "digest": hashlib.sha1(cause).hexdigest()[:12]}
    return json.dumps(body).encode()


def _chains(engine, target):
    return list(engine.chains._open.get(("z2m-test", target), ()))


# -- parsing ------------------------------------------------------------------


def test_parse_accepts_a_report_and_refuses_malformed_ones():
    ok, why = parse_report(report("bulb_a", {"state": "ON"}, cause=b"x"))
    assert why is None and ok.target == "bulb_a" and ok.payload == {"state": "ON"}
    assert ok.reporter == "some-ext" and ok.ref == "op-1" and ok.cause_digest == raw_digest(b"x")
    assert parse_report(b"not json") == (None, "not_json")
    assert parse_report(b"[1]") == (None, "not_object")
    assert parse_report(report("bulb_a", {"state": "ON"}, v=2)) == (None, "version")
    bad_reporter = report("bulb_a", {"state": "ON"}, reporter="bad name!")
    assert parse_report(bad_reporter) == (None, "reporter")
    assert parse_report(report("", {"state": "ON"})) == (None, "target")
    assert parse_report(report("bulb_a", {})) == (None, "payload")
    bad_cause = json.loads(report("bulb_a", {"state": "ON"}, cause=b"x"))
    bad_cause["cause"]["digest"] = "nothex"
    no_cause, _ = parse_report(json.dumps(bad_cause).encode())
    # A bad cause drops the cause, not the report.
    assert no_cause is not None and no_cause.cause_digest is None


def test_cause_ring_forgets_by_age_and_by_size():
    clock = FakeClock()
    ring = CauseRing(clock=clock)
    ring.note("z2m-test", CAUSE_TOPIC, b'{"a": 1}')
    found = ring.lookup("z2m-test", raw_digest(b'{"a": 1}'))
    assert found == (1000.0, CAUSE_TOPIC, correlation_digest(b'{"a": 1}'))
    assert ring.lookup("z2m-other", raw_digest(b'{"a": 1}')) is None, "keyed per instance"
    clock.now += CAUSE_TTL_SECONDS + 1
    assert ring.lookup("z2m-test", raw_digest(b'{"a": 1}')) is None
    for i in range(CAUSE_MAX_ENTRIES + 10):
        ring.note("z2m-test", CAUSE_TOPIC, str(i).encode())
    assert len(ring) == CAUSE_MAX_ENTRIES
    assert ring.lookup("z2m-test", raw_digest(b"0")) is None, "the oldest went first"


# -- intake -------------------------------------------------------------------


def test_a_report_opens_a_set_chain_judged_like_a_set(client):
    engine = _engine(client)
    engine.on_message(REPORT_TOPIC, report("bulb_a", {"state": "ON", "brightness": 10}))
    chain = _chains(engine, "bulb_a")[-1]
    assert chain.verb == "set" and chain.source == "ext:some-ext"
    assert chain.noop_verdict == VERDICT_UNKNOWN, "nothing reported yet"
    engine.on_message("z2m-test/bulb_a", b'{"state": "ON", "brightness": 10}')
    assert chain.echoes == 1, "the device's state after the frame is this chain's echo"
    engine.on_message(REPORT_TOPIC, report("bulb_a", {"state": "ON", "brightness": 10}))
    assert _chains(engine, "bulb_a")[-1].noop_verdict == VERDICT_NOOP
    engine.on_message(REPORT_TOPIC, report("bulb_a", {"brightness": 40}))
    assert _chains(engine, "bulb_a")[-1].noop_verdict == VERDICT_CHANGING
    assert engine.external_commands_status()["reports"] == 3


def test_a_report_and_a_set_are_judged_against_each_other(client):
    engine = _engine(client)
    engine.on_message("z2m-test/bulb_b/set", b'{"brightness": 30}')
    engine.on_message("z2m-test/bulb_b", b'{"brightness": 30}')
    engine.on_message(REPORT_TOPIC, report("bulb_b", {"brightness": 30}))
    assert _chains(engine, "bulb_b")[-1].noop_verdict == VERDICT_NOOP
    assert _chains(engine, "bulb_b")[-1].source == "ext:some-ext"
    assert _chains(engine, "bulb_b")[0].source is None, "an MQTT command keeps a NULL source"


def test_a_report_on_an_unknown_target_opens_nothing(client):
    engine = _engine(client)
    engine.on_message(REPORT_TOPIC, report("no_such_device", {"state": "ON"}))
    assert not _chains(engine, "no_such_device")
    status = engine.external_commands_status()
    assert status["unknown_target"] == 1 and status["reports"] == 0


def test_a_malformed_report_is_counted_not_raised(client):
    engine = _engine(client)
    engine.on_message(REPORT_TOPIC, b"{not json")
    assert engine.external_commands_status()["refused_not_json"] == 1


def test_a_group_report_is_a_group_chain(client):
    engine = _engine(client)
    engine.on_message(REPORT_TOPIC, report("plate_group", {"brightness": 90, "transition": 0.2}))
    chain = _chains(engine, "plate_group")[-1]
    assert chain.source == "ext:some-ext"
    engine.on_message("z2m-test/bulb_a", b'{"brightness": 90}')
    assert chain.echoes == 1, "a member's echo is claimed by the group chain"


# -- attribution through the cause ------------------------------------------


def _script_publish(engine, topic, payload_text, name="Lift Script"):
    engine.ha_attr.handle_event({"event_type": "script_started", "data": {"name": name},
                                 "context": {"id": "ctx-1", "parent_id": None, "user_id": None}})
    return engine.ha_attr.handle_event({
        "event_type": "call_service",
        "data": {"domain": "mqtt", "service": "publish",
                 "service_data": {"topic": topic, "payload": payload_text}},
        "context": {"id": "ctx-1", "parent_id": None, "user_id": None}})


def test_the_cause_names_the_report_when_the_ha_publish_came_first(client):
    engine = _engine(client)
    cause = b'{"id": "op-1", "dev": "bulb_a"}'  # spaced, as Home Assistant's tojson writes it
    _script_publish(engine, CAUSE_TOPIC, cause.decode())
    engine.on_message(CAUSE_TOPIC, cause)
    engine.on_message(REPORT_TOPIC, report("bulb_a", {"state": "ON"}, cause=cause))
    assert _chains(engine, "bulb_a")[-1].client == "script: Lift Script"
    assert engine.external_commands_status()["named"] == 1


def test_a_later_ha_publish_names_every_chain_of_its_cause(client):
    engine = _engine(client)
    cause = b'{"id": "op-2"}'
    engine.on_message(CAUSE_TOPIC, cause)
    engine.on_message(REPORT_TOPIC, report("bulb_a", {"state": "ON"}, cause=cause))
    engine.on_message(REPORT_TOPIC, report("plate_group", {"brightness": 5}, cause=cause))
    # No cause: never named this way.
    engine.on_message(REPORT_TOPIC, report("lamp", {"state": "ON"}))
    assert _chains(engine, "bulb_a")[-1].client is None
    engine._on_ha_publish(CAUSE_TOPIC, "script: Late Script", correlation_digest(cause))
    assert _chains(engine, "bulb_a")[-1].client == "script: Late Script"
    assert _chains(engine, "plate_group")[-1].client == "script: Late Script"
    assert _chains(engine, "lamp")[-1].client is None
    assert engine.external_commands_status()["cause_backfilled"] == 2


def test_a_cause_never_seen_leaves_the_chain_unnamed_and_counted(client):
    engine = _engine(client)
    engine.on_message(REPORT_TOPIC, report("bulb_a", {"state": "ON"}, cause=b"never published"))
    assert _chains(engine, "bulb_a")[-1].client is None
    assert engine.external_commands_status()["cause_unseen"] == 1


def test_name_for_at_finds_a_publish_inside_the_window_around_the_cause():
    from zigbee_ninja.ingest.hacontrol import HaAttribution

    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event({"event_type": "script_started", "data": {"name": "S"},
                              "context": {"id": "c", "parent_id": None, "user_id": None}})
    attribution.handle_event({"event_type": "call_service",
                              "data": {"domain": "mqtt", "service": "publish",
                                       "service_data": {"topic": "t", "payload": '{"a": 1}'}},
                              "context": {"id": "c", "parent_id": None, "user_id": None}})
    digest = correlation_digest(b'{"a": 1}')
    clock.now += 10.0  # the report comes long after: measured from now, the window has closed
    assert attribution.name_for("t", digest) is None
    assert attribution.name_for("t", digest, at=1000.5) == "script: S"
    assert attribution.name_for("t", digest, at=1000.0 + 20.0) is None, "too far after the publish"


# -- non-entity topics ---------------------------------------------------------


def test_an_extensions_own_topic_is_not_device_state(client):
    engine = _engine(client)
    engine.on_message("z2m-test/someext/result/bar", b'{"ok": true}')
    engine.on_message("z2m-test/bulb_a", b'{"state": "ON"}')
    autonomous = {key[2] for key in engine._ledger_autonomous}
    assert "bulb_a" in autonomous and "someext/result/bar" not in autonomous
    assert len(engine.causes) == 1, "remembered as a possible cause"


def test_entity_topics_including_endpoints_and_slash_names_stay_state():
    registry = Registry()
    registry.handle("z2m-test/bridge/devices", json.dumps([
        {"ieee_address": "0x01", "friendly_name": "lamp", "type": "Router"},
        {"ieee_address": "0x02", "friendly_name": "floor/lamp", "type": "Router"}]).encode())
    # The group list not seen yet: assume state, as before this change.
    assert registry.is_entity_topic("z2m-test", "someext/op")
    groups = [{"id": 3, "friendly_name": "lights", "members": []}]
    registry.handle("z2m-test/bridge/groups", json.dumps(groups).encode())
    for suffix in ("lamp", "lamp/top", "floor/lamp", "floor/lamp/top", "lights"):
        assert registry.is_entity_topic("z2m-test", suffix), suffix
    assert not registry.is_entity_topic("z2m-test", "someext/op")
    assert registry.is_entity("z2m-test", "lights") and registry.is_entity("z2m-test", "3")
    assert not registry.is_entity("z2m-test", "someext")


# -- persistence -----------------------------------------------------------------


def test_the_source_is_stored_with_the_chain(client):
    engine = _engine(client)
    engine.on_message(REPORT_TOPIC, report("lamp", {"state": "ON"}))
    engine.on_message("z2m-test/bulb_a/set", b'{"state": "ON"}')
    with engine.chains._mutex:
        engine.chains._expire(time.time() + 60)
    engine.flush_rollups()
    rows = engine._db.connect().execute(
        "SELECT target, source FROM chains ORDER BY target"
    ).fetchall()
    stored = [(r["target"], r["source"]) for r in rows]
    assert stored == [("bulb_a", None), ("lamp", "ext:some-ext")]
