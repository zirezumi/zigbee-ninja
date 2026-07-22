"""Retry hotspots: §V2-5 detector 6, measured per-hop retry rates joined
with the stored topology (V2_PROPOSAL.md)."""

from zigbee_ninja.recommend import retry_hotspots
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 1_700_000_000.0
COORD = "0xc0"


def node(ieee, name, kind, nwk):
    return {"ieeeAddr": ieee, "friendlyName": name, "type": kind, "networkAddress": nwk}


def raw_map():
    return {
        "nodes": [
            node(COORD, "Coordinator", "Coordinator", 0),
            node("0x01", "far_bulb", "Router", 1),
            node("0x02", "busy_relay", "Router", 2),
            node("0x03", "leaf", "EndDevice", 3),
        ],
        "links": [
            # Coordinator's own hop to far_bulb is weak: the measured counter's territory.
            {"sourceIeeeAddr": "0x01", "targetIeeeAddr": COORD, "lqi": 54},
            # busy_relay carries routes and its leaf link is weak.
            {
                "sourceIeeeAddr": "0x02",
                "targetIeeeAddr": COORD,
                "lqi": 200,
                "routes": [
                    {"status": "ACTIVE", "nextHopAddress": 2},
                    {"status": "ACTIVE", "nextHopAddress": 2},
                    {"status": "ACTIVE", "nextHopAddress": 2},
                ],
            },
            {"sourceIeeeAddr": "0x03", "targetIeeeAddr": "0x02", "lqi": 60},
        ],
    }


def context(tmp_path, retry_rates, topo=None, chains=200, utilization=None):
    """utilization: {instance: {...}} pressure, used for significance banding."""
    db = Database(tmp_path)
    conn = db.connect()
    for i in range(chains):
        conn.execute(
            "INSERT INTO chains (instance, target, verb, opened_at, client, "
            "payload_size, echo_count, redundant) "
            "VALUES ('z2m-a', 'far_bulb', 'set', ?, 'ha', 10, 0, 0)",
            (NOW - 3600 + i,),
        )
    conn.commit()
    return DetectorContext(
        conn=conn,
        now=NOW,
        lookback_seconds=86400.0,
        instances=list(retry_rates),
        instance_info={
            base: {"base_topic": base, "coordinator_ieee": COORD}
            for base in retry_rates
        },
        knees={},
        is_group=lambda base, target: False,
        group_members=lambda base, target: [],
        groups=lambda base: [],
        devices=lambda base: [],
        router_count_for=lambda base: 2,
        pricing=lambda base: (None, retry_rates[base]),
        db=db,
        topology_latest=(lambda base: {"raw": topo}) if topo else (lambda base: {}),
        utilization=utilization or {},
    )


def test_low_or_unmeasured_rates_stay_silent(tmp_path):
    ctx = context(tmp_path, {"z2m-a": 0.02, "z2m-b": None}, topo=raw_map())
    assert retry_hotspots.detect(ctx) == []


def test_elevated_rate_names_coordinator_hop_and_relay(tmp_path):
    ctx = context(tmp_path, {"z2m-a": 0.11}, topo=raw_map())
    findings = retry_hotspots.detect(ctx)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "retry_hotspots"
    assert finding.subject == "z2m-a"
    assert finding.confidence == "low"
    assert finding.action["kind"] == "investigate"
    assert "far_bulb" in finding.action["suspects"]
    assert "busy_relay" in finding.action["suspects"]
    assert "11%" in finding.finding
    assert "far_bulb" in finding.finding
    assert "busy_relay" in finding.finding
    assert finding.saving["us_per_s"] > 0
    assert finding.saving["provenance"] == "modeled"
    weak = next(e for e in finding.evidence if e["kind"] == "weak_links")
    assert weak["coordinator_hop"][0] == {"device": "far_bulb", "lqi": 54}
    assert weak["heavy_relays"][0]["device"] == "busy_relay"
    assert weak["heavy_relays"][0]["routes_via"] == 3


def test_without_snapshot_the_finding_says_to_pull_one(tmp_path):
    ctx = context(tmp_path, {"z2m-a": 0.08})
    findings = retry_hotspots.detect(ctx)
    assert len(findings) == 1
    assert "No topology snapshot" in findings[0].finding
    weak = next(e for e in findings[0].evidence if e["kind"] == "weak_links")
    assert weak["snapshot_present"] is False


def test_coordinator_never_names_itself_a_relay_and_suspects_dedupe(tmp_path):
    topo = raw_map()
    # The coordinator terminates routes over its own weak link: it must not
    # appear as a relay suspect (it is the measuring node), and a device on
    # both suspect paths appears once.
    topo["links"][0]["routes"] = [
        {"status": "ACTIVE", "nextHopAddress": 0},
        {"status": "ACTIVE", "nextHopAddress": 0},
        {"status": "ACTIVE", "nextHopAddress": 0},
    ]
    topo["links"].append({"sourceIeeeAddr": "0x03", "targetIeeeAddr": "0x01", "lqi": 60})
    ctx = context(tmp_path, {"z2m-a": 0.11}, topo=topo)
    findings = retry_hotspots.detect(ctx)
    assert len(findings) == 1
    suspects = findings[0].action["suspects"]
    assert "Coordinator" not in suspects
    assert len(suspects) == len(set(suspects))
    weak = next(e for e in findings[0].evidence if e["kind"] == "weak_links")
    assert all(r["device"] != "Coordinator" for r in weak["heavy_relays"])


def test_investigation_declares_that_it_costs_nothing(tmp_path):
    # The action kind is `investigate`, and looking costs the mesh nothing.
    # Declaring that beats omitting it: a reader has to be able to tell
    # "this is free" apart from "nobody priced this".
    ctx = context(tmp_path, {"z2m-a": 0.11}, topo=raw_map())
    (finding,) = retry_hotspots.detect(ctx)
    assert finding.cost["denominator"] is None
    assert finding.cost["raises_load"] is False
    assert "costs nothing on the mesh" in finding.cost["note"]


def test_retry_saving_bands_against_the_channel_budget(tmp_path):
    ctx = context(
        tmp_path,
        {"z2m-a": 0.11},
        topo=raw_map(),
        utilization={"z2m-a": {"channel_budget_pct": 0.4}},
    )
    (finding,) = retry_hotspots.detect(ctx)
    assert finding.significance["denominator"] == "channel airtime"
    assert finding.significance["band"] == "low"
    assert "not under pressure" in finding.significance["rationale"]


def test_retry_significance_is_unknown_before_a_calibration(tmp_path):
    ctx = context(tmp_path, {"z2m-a": 0.11}, topo=raw_map())
    (finding,) = retry_hotspots.detect(ctx)
    assert finding.significance["band"] == "unknown"
    assert "not measured yet" in finding.significance["rationale"]


def test_healthy_coordinator_links_shift_the_story(tmp_path):
    topo = raw_map()
    topo["links"][0]["lqi"] = 220  # coordinator hop healthy; relay still weak
    ctx = context(tmp_path, {"z2m-a": 0.09}, topo=topo)
    findings = retry_hotspots.detect(ctx)
    assert len(findings) == 1
    assert "no weak link on the coordinator's own hops" in findings[0].finding
    assert "busy_relay" in findings[0].finding
