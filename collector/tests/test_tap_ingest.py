import pytest

from tests.test_ezsp_params import APS, LIVE_INCOMING_LOOPBACK, LIVE_INCOMING_RADIO
from tests.test_pcap_cli import COORD, HOST, build_pcap, conversation, tcp_packet
from zigbee_ninja.capacity import airtime
from zigbee_ninja.ingest.tap import TapIngest


def resolve(ip, port):
    # COORD is ("10.0.0.50", 6638) in the synthetic conversation fixture.
    return "z2m-test" if (ip, port) == COORD else None


def test_tap_decodes_stream_to_ezsp_frames():
    pcap, _ = conversation()
    tap = TapIngest(resolve_instance=resolve)
    tap.register_agent("agent-1", {"iface": "vmbr0", "filter": "tcp port 6638"})
    tap.feed("agent-1", pcap)

    stats = tap.stats()
    assert stats["agents"] == 1
    assert stats["agent_details"][0]["meta"]["iface"] == "vmbr0"
    assert len(stats["flows"]) == 1
    flow = stats["flows"][0]
    assert flow["instance"] == "z2m-test"
    assert flow["protocol_version"] == 13
    assert flow["ezsp_frames"]["sendUnicast"] == 1
    assert flow["ezsp_frames"]["incomingMessageHandler"] == 1
    assert flow["crc_errors"] == 0


def test_tap_survives_chunked_delivery():
    pcap, _ = conversation()
    tap = TapIngest(resolve_instance=resolve)
    tap.register_agent("agent-1", {})
    for i in range(0, len(pcap), 7):  # odd chunk size across record boundaries
        tap.feed("agent-1", pcap[i : i + 7])
    flow = tap.stats()["flows"][0]
    assert flow["data_frames"] == 4  # 2 to-coord + 2 from-coord DATA frames


def test_unknown_flow_ignored():
    def resolve_none(ip, port):
        return None

    tap = TapIngest(resolve_instance=resolve_none)
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", build_pcap([]))
    assert tap.stats()["flows"] == []


def test_drop_agent_clears_reader():
    tap = TapIngest(resolve_instance=resolve)
    tap.register_agent("agent-1", {})
    tap.drop_agent("agent-1")
    assert tap.stats()["agents"] == 0
    tap.feed("agent-1", b"ignored")  # must not raise


def _ext(seq: int, ctrl_lo: int, frame_id: int, params: bytes) -> bytes:
    """Extended-header EZSP envelope (ctrl_hi = frame-format version 1)."""
    return bytes([seq, ctrl_lo, 0x01, frame_id & 0xFF, frame_id >> 8]) + params


def v14_wire_conversation() -> bytes:
    """One coordinator flow exercising the deep-parsed v14-era frames."""
    from zigbee_ninja.decode.ash import encode_data_frame

    su_cmd = _ext(1, 0x00, 0x0034,
                  bytes([0x00, 0xCD, 0x4D]) + APS + bytes([0x34, 0x12, 0x03, 1, 2, 3]))
    ms_ok = _ext(2, 0x90, 0x003F,
                 bytes([0, 0, 0, 0, 0x00, 0xCD, 0x4D]) + APS + bytes([0x34, 0x12, 0x00]))
    mc_cmd = _ext(3, 0x00, 0x0038,
                  APS[:8] + bytes([0x0A, 0x00, 0x55]) + bytes.fromhex("0c0000ffff00")
                  + bytes([0x78, 0x56, 0x02, 0xAA, 0xBB]))
    in_loop = _ext(4, 0x90, 0x0045, LIVE_INCOMING_LOOPBACK)
    in_radio = _ext(5, 0x90, 0x0045, LIVE_INCOMING_RADIO)
    ms_mc = _ext(6, 0x90, 0x003F,
                 bytes([0, 0, 0, 0, 0x02, 0x0A, 0x00]) + APS + bytes([0x78, 0x56, 0x00]))
    route_rec = _ext(7, 0x90, 0x0059,
                     bytes.fromhex("2a1b") + bytes(8) + bytes([0x90, 0xB0, 0x01, 0x11, 0x22]))
    counter_values = [0] * 40
    counter_values[1] = 82   # mac_tx_broadcast
    counter_values[3] = 812  # mac_tx_unicast_success
    counters_rsp = _ext(8, 0x80, 0x0065,
                        b"".join(v.to_bytes(2, "little") for v in counter_values))

    packets = []
    host_seq, coord_seq = 5000, 9000
    frm_to = frm_from = 0

    def send(ts: float, payload: bytes, to_coord: bool):
        nonlocal host_seq, coord_seq, frm_to, frm_from
        if to_coord:
            wire = encode_data_frame(payload, frm_num=frm_to, ack_num=frm_from)
            frm_to = (frm_to + 1) % 8
            packets.append((ts, tcp_packet(HOST, COORD, host_seq, wire)))
            host_seq += len(wire)
        else:
            wire = encode_data_frame(payload, frm_num=frm_from, ack_num=frm_to)
            frm_from = (frm_from + 1) % 8
            packets.append((ts, tcp_packet(COORD, HOST, coord_seq, wire)))
            coord_seq += len(wire)

    send(200.00, su_cmd, True)
    send(200.05, ms_ok, False)     # unicast delivery confirm 50 ms later
    send(200.10, mc_cmd, True)
    send(200.12, in_loop, False)   # our own groupcast echoed back — not radio
    send(200.20, in_radio, False)
    send(200.25, ms_mc, False)     # groupcast confirm — no latency sample
    send(200.30, route_rec, False)
    send(200.35, counters_rsp, False)  # Z2M's own counter poll, harvested passively
    return build_pcap(packets)


def test_wire_telemetry_airtime_latency_and_counters():
    now = [1000.0]
    tap = TapIngest(
        resolve_instance=resolve, router_count=lambda _base: 4, clock=lambda: now[0]
    )
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", v14_wire_conversation())

    now[0] = 1002.0  # step past the recording second so the 60 s view sees it
    stats = tap.stats()
    flow = stats["flows"][0]
    assert flow["ezsp_frames"]["incomingRouteRecordHandler"] == 1

    wire = flow["wire"]
    assert wire["delivery_ok"] == 2
    assert wire["delivery_failed"] == 0
    assert wire["statuses"] == {"0x0000": 2}
    assert wire["loopbacks"] == 1
    assert wire["route_records"] == 1
    assert wire["layout_mismatch"] == 0
    assert wire["incoming_trailing"] == {"02": 1}
    assert wire["pending_sends"] == 0  # both sends were confirmed
    assert wire["lqi_ewma"] == pytest.approx(109.8, abs=0.1)   # 108 then EWMA(144)
    assert wire["rssi_ewma"] == pytest.approx(-73.35, abs=0.1)
    assert wire["counters"] == {"mac_tx_broadcast": 82, "mac_tx_unicast_success": 812}
    assert wire["counters_at"] == 1000.0
    assert wire["counters_provenance"] == "inferred"

    latency = stats["latency"]["z2m-test"]
    assert latency["count"] == 1  # unicast only; the groupcast confirm is excluded
    assert latency["p50_ms"] == pytest.approx(50.0, abs=0.5)

    buckets = stats["airtime"]["z2m-test"]["buckets"]
    assert buckets["tx_unicast"]["airtime_us_60s"] == airtime.unicast_airtime_us(3)
    assert buckets["tx_groupcast"]["airtime_us_60s"] == pytest.approx(
        airtime.groupcast_airtime_us(2, n_routers=4)
    )
    assert buckets["rx"]["airtime_us_60s"] == airtime.incoming_airtime_us(
        5, group_addressed=False, acked=True
    )
    assert buckets["rx_mesh"]["airtime_us_60s"] == airtime.route_record_airtime_us(1)
    assert all(b["frames_60s"] == 1 for b in buckets.values())
    assert stats["airtime"]["z2m-test"]["budget_pct_60s"] > 0

    now[0] = 1012.0  # cross a 10 s boundary: the window drains exactly once
    rows = tap.airtime.drain_completed_windows()
    assert {(row[1], row[2]) for row in rows} == {
        ("z2m-test", "tx_unicast"),
        ("z2m-test", "tx_groupcast"),
        ("z2m-test", "rx"),
        ("z2m-test", "rx_mesh"),
    }
    assert all(row[0] == 1000 for row in rows)
    assert tap.airtime.drain_completed_windows() == []

    latency_rows = tap.latency.drain_completed_windows()
    assert latency_rows == [(1000, "z2m-test", 1, 50.0, 50.0, 50.0)]
    assert tap.latency.drain_completed_windows() == []


def test_passive_avg_tx_from_counter_windows():
    """readAndClearCounters responses are per-window deltas: the coordinator's
    own broadcast TX counters yield avg_tx passively (supersedes the §11
    groupcast stage), and groupcast airtime switches to the measured value."""
    from zigbee_ninja.decode.ash import encode_data_frame

    def counters_frame(seq: int, mac_tx: int, aps_tx: int, mtorr: int) -> bytes:
        values = [0] * 40
        values[1] = mac_tx  # mac_tx_broadcast
        values[7] = aps_tx  # aps_data_tx_broadcast
        values[12] = mtorr  # route_discovery_initiated (MTORR)
        return _ext(seq, 0x80, 0x0065, b"".join(v.to_bytes(2, "little") for v in values))

    # window 600 s → link-status estimate 40 → (460 − 40) / (180 + 20) = 2.1
    c1 = counters_frame(1, mac_tx=82, aps_tx=60, mtorr=10)
    c2 = counters_frame(2, mac_tx=460, aps_tx=180, mtorr=20)
    # Too few originated broadcasts for a sample — must not move the EWMA.
    c3 = counters_frame(3, mac_tx=100, aps_tx=5, mtorr=2)
    mc = _ext(4, 0x00, 0x0038,
              APS[:8] + bytes([0x0A, 0x00, 0x55]) + bytes.fromhex("0c0000ffff00")
              + bytes([0x78, 0x56, 0x02, 0xAA, 0xBB]))
    # Residual beyond the passive-ack maximum (raw 9.8): relay-contaminated —
    # discarded and counted, never clamped into the EWMA.
    c4 = counters_frame(5, mac_tx=2000, aps_tx=180, mtorr=20)

    packets = []
    host_seq, coord_seq = 5000, 9000
    frm_to = frm_from = 0

    def send(ts: float, payload: bytes, to_coord: bool):
        nonlocal host_seq, coord_seq, frm_to, frm_from
        if to_coord:
            wire = encode_data_frame(payload, frm_num=frm_to, ack_num=frm_from)
            frm_to = (frm_to + 1) % 8
            packets.append((ts, tcp_packet(HOST, COORD, host_seq, wire)))
            host_seq += len(wire)
        else:
            wire = encode_data_frame(payload, frm_num=frm_from, ack_num=frm_to)
            frm_from = (frm_from + 1) % 8
            packets.append((ts, tcp_packet(COORD, HOST, coord_seq, wire)))
            coord_seq += len(wire)

    send(100.0, c1, False)
    send(700.0, c2, False)
    send(1300.0, c3, False)
    send(1300.1, mc, True)
    send(1900.0, c4, False)

    prefix_a = build_pcap(packets[:1])
    prefix_b = build_pcap(packets[:2])
    prefix_c = build_pcap(packets[:4])
    full = build_pcap(packets)

    now = [1000.0]
    tap = TapIngest(
        resolve_instance=resolve, router_count=lambda _base: 4, clock=lambda: now[0]
    )
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", prefix_a)  # first harvest: timestamp only, no sample
    now[0] = 1600.0
    tap.feed("agent-1", prefix_b[len(prefix_a):])  # second harvest → sample 2.1
    now[0] = 2200.0
    tap.feed("agent-1", prefix_c[len(prefix_b):])  # guarded harvest + groupcast send

    now[0] = 2202.0
    stats = tap.stats()
    wire = stats["flows"][0]["wire"]
    assert wire["avg_tx"] == 2.1
    assert wire["avg_tx_samples"] == 1  # c1 seeds, c2 samples, c3 is guarded out
    assert wire["avg_tx_last"]["sample"] == 2.1
    assert wire["avg_tx_last"]["accepted"] is True
    assert wire["avg_tx_last"]["window_seconds"] == 600.0
    assert wire["avg_tx_provenance"].startswith("measured")

    buckets = stats["airtime"]["z2m-test"]["buckets"]
    assert buckets["tx_groupcast"]["airtime_us_60s"] == pytest.approx(
        airtime.groupcast_airtime_us(2, n_routers=4, avg_tx=2.1)
    )

    now[0] = 2800.0
    tap.feed("agent-1", full[len(prefix_c):])  # contaminated harvest (raw 9.8)
    wire = tap.stats()["flows"][0]["wire"]
    assert wire["avg_tx"] == 2.1  # EWMA untouched
    assert wire["avg_tx_samples"] == 1
    assert wire["avg_tx_rejected"] == 1
    assert wire["avg_tx_last"]["accepted"] is False
    assert wire["avg_tx_last"]["reason"] == "relay_contaminated"
    assert wire["avg_tx_last"]["raw"] == pytest.approx(9.8, abs=0.01)


def test_passive_retry_rate_scales_unicast_airtime():
    """Each clearing counter read is a self-contained window: the coordinator's
    own MAC unicast retry/success ratio feeds the §10 (1 + retry_rate) term,
    and unicast TX airtime recorded afterwards carries the measured
    multiplier."""
    from zigbee_ninja.decode.ash import encode_data_frame

    def counters_frame(seq: int, success: int, retries: int) -> bytes:
        values = [0] * 40
        values[3] = success  # mac_tx_unicast_success
        values[4] = retries  # mac_tx_unicast_retry
        return _ext(seq, 0x80, 0x0065, b"".join(v.to_bytes(2, "little") for v in values))

    c1 = counters_frame(1, success=800, retries=80)  # 10% → sample 0.1
    # Below the success floor: must not move the EWMA.
    c2 = counters_frame(2, success=10, retries=30)
    su = _ext(3, 0x00, 0x0034,
              bytes([0x00, 0xCD, 0x4D]) + APS + bytes([0x34, 0x12, 0x03, 1, 2, 3]))

    packets = []
    host_seq, coord_seq = 5000, 9000
    frm_to = frm_from = 0

    def send(ts: float, payload: bytes, to_coord: bool):
        nonlocal host_seq, coord_seq, frm_to, frm_from
        if to_coord:
            wire = encode_data_frame(payload, frm_num=frm_to, ack_num=frm_from)
            frm_to = (frm_to + 1) % 8
            packets.append((ts, tcp_packet(HOST, COORD, host_seq, wire)))
            host_seq += len(wire)
        else:
            wire = encode_data_frame(payload, frm_num=frm_from, ack_num=frm_to)
            frm_from = (frm_from + 1) % 8
            packets.append((ts, tcp_packet(COORD, HOST, coord_seq, wire)))
            coord_seq += len(wire)

    send(100.0, c1, False)
    send(200.0, c2, False)
    send(300.0, su, True)

    now = [1000.0]
    tap = TapIngest(
        resolve_instance=resolve, router_count=lambda _base: 4, clock=lambda: now[0]
    )
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", build_pcap(packets))

    now[0] = 1002.0
    stats = tap.stats()
    wire = stats["flows"][0]["wire"]
    assert wire["retry_rate"] == 0.1
    assert wire["retry_rate_samples"] == 1  # the tiny window is guarded out
    assert wire["retry_rate_last"] == {
        "sample": 0.1,
        "mac_tx_unicast_success": 800,
        "mac_tx_unicast_retry": 80,
    }
    assert wire["retry_rate_provenance"].startswith("measured")

    buckets = stats["airtime"]["z2m-test"]["buckets"]
    assert buckets["tx_unicast"]["airtime_us_60s"] == pytest.approx(
        airtime.unicast_airtime_us(3, retry_rate=0.1)
    )


def test_incoming_zcl_callback_feeds_fusion_not_loopbacks():
    """Radio incoming frames with a ZCL header reach the fusion callback with
    (sender, zcl_seq, pcap_ts); loopbacks and ZDO-style frames never do."""
    seen = []
    tap = TapIngest(
        resolve_instance=resolve,
        router_count=lambda _base: 4,
        on_zcl_incoming=lambda instance, sender, seq, ts: seen.append(
            (instance, sender, seq, ts)
        ),
    )
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", v14_wire_conversation())
    # The conversation carries one radio incoming (ZCL seq 0x92 from 0x01E6 at
    # pcap ts 200.20) and one loopback, which must not fuse.
    assert seen == [("z2m-test", 0x01E6, 0x92, 200.20)]


def test_avg_tx_accepts_hourly_counter_windows():
    """Z2M's ember watchdog polls counters on a fixed 1 h setInterval, so real
    windows are ~3600 s plus jitter — the old 3600 s ceiling rejected nearly
    every live sample (observed gaps: 3599–3615 s)."""
    from zigbee_ninja.decode.ash import encode_data_frame

    def counters_frame(seq: int, mac_tx: int, aps_tx: int, mtorr: int) -> bytes:
        values = [0] * 40
        values[1] = mac_tx
        values[7] = aps_tx
        values[12] = mtorr
        return _ext(seq, 0x80, 0x0065, b"".join(v.to_bytes(2, "little") for v in values))

    c1 = counters_frame(1, mac_tx=100, aps_tx=50, mtorr=10)
    # 3615 s window → link estimate 241 → (901 − 241) / (240 + 60) = 2.2
    c2 = counters_frame(2, mac_tx=901, aps_tx=240, mtorr=60)

    packets = []
    coord_seq = 9000
    for index, (ts, frame) in enumerate(((100.0, c1), (3715.0, c2))):
        wire = encode_data_frame(frame, frm_num=index, ack_num=0)
        packets.append((ts, tcp_packet(COORD, HOST, coord_seq, wire)))
        coord_seq += len(wire)

    prefix = build_pcap(packets[:1])
    full = build_pcap(packets)
    now = [1000.0]
    tap = TapIngest(
        resolve_instance=resolve, router_count=lambda _base: 4, clock=lambda: now[0]
    )
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", prefix)
    now[0] = 1000.0 + 3615.0
    tap.feed("agent-1", full[len(prefix):])

    wire_stats = tap.stats()["flows"][0]["wire"]
    assert wire_stats["avg_tx_samples"] == 1
    assert wire_stats["avg_tx_last"]["window_seconds"] == 3615.0
    assert wire_stats["avg_tx"] == pytest.approx(2.2, abs=0.01)


def test_headerless_stream_resets_reader_without_crashing():
    # A reconnect that skips the pcap global header (bad magic) must not crash
    # the handler; the reader resets and later re-syncs on a fresh header.
    tap = TapIngest(resolve_instance=resolve)
    tap.register_agent("agent-1", {})
    tap.feed("agent-1", b"\xde\xad\xbe\xef" * 8)  # not a pcap header
    assert tap.agents["agent-1"]["reader_resets"] >= 1

    pcap, _ = conversation()
    tap.feed("agent-1", pcap)  # fresh header re-syncs
    assert tap.stats()["flows"][0]["instance"] == "z2m-test"
