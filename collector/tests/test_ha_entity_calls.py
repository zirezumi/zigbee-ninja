"""Entity service calls (`light.turn_off` ...) publish through an MQTT entity,
not `mqtt.publish`, so no publish event names their commands. A person's call
on a Home Assistant light group arrives as one such call per member, and every
command it caused was filed "(unattributed)"."""

import json

from test_ha_attribution import SETUP, FakeClock, automation_event

from zigbee_ninja.attribution.chains import AMBIGUOUS_COMMANDER
from zigbee_ninja.ingest.hacontrol import HaAttribution
from zigbee_ninja.ingest.registry import entity_slug

USER = {"id": "ctx-user", "parent_id": None, "user_id": "abc123"}


def light_call(service: str, entity_ids, context: dict) -> dict:
    return {
        "event_type": "call_service",
        "data": {"domain": "light", "service": service, "service_data": {"entity_id": entity_ids}},
        "context": context,
    }


def test_entity_slug_is_the_object_id_home_assistant_derives():
    assert entity_slug("office_couch_sconce/bottom") == "office_couch_sconce_bottom"
    assert entity_slug("Kitchen Ceiling 1") == "kitchen_ceiling_1"
    assert entity_slug("lr__spot--3") == "lr_spot_3"


def test_a_user_call_names_the_member_command_that_follows_it():
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(
        light_call("turn_off", ["light.office_couch_sconce_bottom", "light.lamp"], USER)
    )
    clock.now += 0.2
    assert (
        attribution.entity_name_for("office_couch_sconce/bottom", b'{"state":"OFF"}')
        == "user (UI/API)"
    )
    assert attribution.entity_name_for("lamp", b'{"state":"OFF","transition":1}') == "user (UI/API)"
    assert attribution.entity_name_for("other_lamp", b'{"state":"OFF"}') is None


def test_a_command_the_service_could_not_have_sent_is_not_named():
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(light_call("turn_off", "light.lamp", USER))
    assert attribution.entity_name_for("lamp", b'{"state":"ON"}') is None
    # No state key: nothing contradicts it.
    assert attribution.entity_name_for("lamp", b'{"brightness":10}') == "user (UI/API)"


def test_two_callers_on_one_entity_are_ambiguous():
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(automation_event("Night Sweep", "ctx-a"))
    attribution.handle_event(light_call("turn_off", "light.lamp", {"id": "ctx-a"}))
    attribution.handle_event(light_call("turn_off", "light.lamp", USER))
    assert attribution.entity_name_for("lamp", b'{"state":"OFF"}') == AMBIGUOUS_COMMANDER


def test_an_old_call_names_nothing():
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(light_call("turn_on", "light.lamp", USER))
    clock.now += 3.5
    assert attribution.entity_name_for("lamp", b'{"state":"ON"}') is None


def test_area_targets_and_other_domains_are_ignored():
    calls = []
    attribution = HaAttribution(clock=FakeClock(), on_entity_call=lambda *a: calls.append(a))
    attribution.handle_event(
        {
            "event_type": "call_service",
            "data": {"domain": "light", "service": "turn_off",
                     "service_data": {"area_id": "office"}},
            "context": USER,
        }
    )
    attribution.handle_event(light_call("turn_off", "switch.lamp", USER))
    attribution.handle_event(
        {
            "event_type": "call_service",
            "data": {"domain": "switch", "service": "turn_off",
                     "service_data": {"entity_id": "switch.lamp"}},
            "context": USER,
        }
    )
    assert calls == []
    assert attribution.counters["entity_calls"] == 0


# -- engine: the command reaches the broker before the HA event -----------------

DEVICES = [
    {"ieee_address": "0x01", "friendly_name": "Coordinator", "type": "Coordinator"},
    {"ieee_address": "0x02", "friendly_name": "lamp", "type": "Router"},
    {"ieee_address": "0x03", "friendly_name": "office_couch_sconce/bottom", "type": "Router"},
]


def _engine(client, instances=("z2m-test",)):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    for base in instances:
        engine.registry.handle(f"{base}/bridge/info", b'{"version": "2.3.0"}')
        engine.registry.handle(f"{base}/bridge/devices", json.dumps(DEVICES).encode())
    return engine


def test_engine_backfills_a_member_command_from_the_entity_call(client):
    engine = _engine(client)
    engine.on_message("z2m-test/office_couch_sconce/bottom/set", b'{"state":"OFF"}')
    engine.ha_attr.handle_event(light_call("turn_off", "light.office_couch_sconce_bottom", USER))
    chain = engine.chains._open[("z2m-test", "office_couch_sconce/bottom")][-1]
    assert chain.client == "user (UI/API)"


def test_engine_names_at_wire_time_when_the_event_came_first(client):
    engine = _engine(client)
    engine.ha_attr.handle_event(light_call("turn_off", "light.lamp", USER))
    engine.on_message("z2m-test/lamp/set", b'{"state":"OFF"}')
    assert engine.chains._open[("z2m-test", "lamp")][-1].client == "user (UI/API)"


def test_engine_refuses_a_contradicting_chain(client):
    engine = _engine(client)
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    engine.ha_attr.handle_event(light_call("turn_off", "light.lamp", USER))
    assert engine.chains._open[("z2m-test", "lamp")][-1].client is None


def test_engine_refuses_a_slug_two_instances_share(client):
    engine = _engine(client, instances=("z2m-test", "z2m-other"))
    engine.on_message("z2m-test/lamp/set", b'{"state":"OFF"}')
    engine.ha_attr.handle_event(light_call("turn_off", "light.lamp", USER))
    assert engine.chains._open[("z2m-test", "lamp")][-1].client is None


def test_an_mqtt_publish_still_outranks_an_entity_call(client):
    """Bytes beat a slug: when an `mqtt.publish` explains the command, the
    entity call never gets a say."""
    engine = _engine(client)
    engine.ha_attr.handle_event(light_call("turn_off", "light.lamp", USER))
    engine.ha_attr.handle_event(automation_event("Lamp Driver", "ctx-drv"))
    engine.ha_attr.handle_event(
        {
            "event_type": "call_service",
            "data": {"domain": "mqtt", "service": "publish",
                     "service_data": {"topic": "z2m-test/lamp/set", "payload": '{"state":"OFF"}'}},
            "context": {"id": "ctx-drv"},
        }
    )
    engine.on_message("z2m-test/lamp/set", b'{"state":"OFF"}')
    assert engine.chains._open[("z2m-test", "lamp")][-1].client == "automation: Lamp Driver"
