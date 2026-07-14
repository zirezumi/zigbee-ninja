from tests.test_pcap_cli import conversation
from zigbee_ninja.decode.pcap_stream import StreamingPcapReader


def test_streaming_matches_offline_over_full_buffer():
    pcap, _ = conversation()
    reader = StreamingPcapReader()
    segments = list(reader.feed(pcap))
    assert len(segments) == 6


def test_streaming_across_arbitrary_chunk_boundaries():
    pcap, _ = conversation()
    reader = StreamingPcapReader()
    segments = []
    # Feed one byte at a time — records must only emit once fully arrived.
    for i in range(len(pcap)):
        segments.extend(reader.feed(pcap[i : i + 1]))
    assert len(segments) == 6
    # The two directions and ports are recovered intact.
    ports = {seg.dst[1] for seg in segments} | {seg.src[1] for seg in segments}
    assert 6638 in ports


def test_streaming_header_split():
    pcap, _ = conversation()
    reader = StreamingPcapReader()
    # Global header split mid-way, then the rest.
    assert list(reader.feed(pcap[:10])) == []
    segments = list(reader.feed(pcap[10:]))
    assert len(segments) == 6
