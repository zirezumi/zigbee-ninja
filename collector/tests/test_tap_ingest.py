from tests.test_pcap_cli import COORD, build_pcap, conversation
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
