from zigbee_ninja.ingest.fusion import FusionTracker


def make(clock_value: list[float]) -> FusionTracker:
    return FusionTracker(clock=lambda: clock_value[0])


def test_wire_then_probe_matches_and_measures_offset():
    now = [1000.0]
    fusion = make(now)
    fusion.on_wire("z2m-test", 0x1234, 0x92, pcap_ts=999.950)
    now[0] = 1001.0
    fusion.on_probe("z2m-test", 0x1234, 0x92, probe_ts=999.962)

    view = fusion.snapshot()["z2m-test"]
    assert view["matched_5m"] == 1
    assert view["wire_only_5m"] == 0 and view["probe_only_5m"] == 0
    assert view["clock_offset_ms"] == 12.0  # probe clock 12 ms ahead of pcap
    assert view["offset_samples"] == 1
    assert view["state"] == "fusing"


def test_probe_then_wire_matches_too():
    now = [1000.0]
    fusion = make(now)
    fusion.on_probe("z2m-test", 7, 3, probe_ts=1000.0)
    now[0] = 1001.5
    fusion.on_wire("z2m-test", 7, 3, pcap_ts=1001.4)
    view = fusion.snapshot()["z2m-test"]
    assert view["matched_5m"] == 1
    assert view["probe_only_5m"] == 0


def test_watermark_expiry_counts_disagreement():
    now = [1000.0]
    fusion = make(now)
    fusion.on_wire("z2m-test", 1, 10, pcap_ts=1000.0)
    fusion.on_probe("z2m-test", 2, 20, probe_ts=1000.0)
    now[0] = 1006.0  # past the 5 s watermark
    view = fusion.snapshot()["z2m-test"]
    assert view["wire_only_5m"] == 1
    assert view["probe_only_5m"] == 1
    assert view["matched_5m"] == 0


def test_expired_entry_never_matches_late_counterpart():
    now = [1000.0]
    fusion = make(now)
    fusion.on_wire("z2m-test", 1, 10, pcap_ts=1000.0)
    now[0] = 1006.0
    fusion.on_probe("z2m-test", 1, 10, probe_ts=1006.0)  # too late: new pending
    now[0] = 1012.0
    view = fusion.snapshot()["z2m-test"]
    assert view["matched_5m"] == 0
    assert view["wire_only_5m"] == 1
    assert view["probe_only_5m"] == 1


def test_state_reports_awaiting_probe_when_only_wire_flows():
    now = [1000.0]
    fusion = make(now)
    fusion.on_wire("z2m-test", 1, 10, pcap_ts=1000.0)
    assert fusion.snapshot()["z2m-test"]["state"] == "awaiting probe v0.4"

    # Sequenced probe events flip the state; long silence on both sides idles.
    fusion.on_probe("z2m-test", 9, 9, probe_ts=1000.0)
    assert fusion.snapshot()["z2m-test"]["state"] == "fusing"
    now[0] = 2000.0
    assert fusion.snapshot()["z2m-test"]["state"] == "idle"


def test_top_unmatched_attributes_failures_per_sender_and_side():
    now = [1000.0]
    fusion = make(now)
    # Sender 7 fuses fine; sender 9 disagrees on the key from both sides.
    fusion.on_wire("z2m-test", 7, 1, pcap_ts=1000.0)
    fusion.on_probe("z2m-test", 7, 1, probe_ts=1000.1)
    fusion.on_wire("z2m-test", 9, 10, pcap_ts=1000.0)
    fusion.on_wire("z2m-test", 9, 11, pcap_ts=1000.1)
    fusion.on_probe("z2m-test", 9, 200, probe_ts=1000.2)
    now[0] = 1006.0
    view = fusion.snapshot()["z2m-test"]
    assert view["top_unmatched"] == [
        {"nwk": 9, "wire_only": 2, "probe_only": 1, "matched": 0}
    ]


def test_seq_delta_histogram_exposes_systematic_shift():
    now = [1000.0]
    fusion = make(now)
    # The same sender expires unmatched on both sides with sequences that
    # differ by a constant 100: the histogram must say so.
    for offset in range(4):
        fusion.on_wire("z2m-test", 5, 10 + offset, pcap_ts=1000.0 + offset / 10)
        fusion.on_probe("z2m-test", 5, 110 + offset, probe_ts=1000.0 + offset / 10)
    now[0] = 1006.0
    view = fusion.snapshot()["z2m-test"]
    assert view["matched_5m"] == 0
    assert view["seq_delta_histogram"].get(100, 0) >= 3


def test_same_key_twice_pairs_each_occurrence_once():
    now = [1000.0]
    fusion = make(now)
    fusion.on_wire("z2m-test", 5, 42, pcap_ts=1000.0)
    fusion.on_wire("z2m-test", 5, 42, pcap_ts=1000.1)
    fusion.on_probe("z2m-test", 5, 42, probe_ts=1000.2)
    fusion.on_probe("z2m-test", 5, 42, probe_ts=1000.3)
    view = fusion.snapshot()["z2m-test"]
    assert view["matched_5m"] == 2
    assert view["wire_only_5m"] == 0 and view["probe_only_5m"] == 0
