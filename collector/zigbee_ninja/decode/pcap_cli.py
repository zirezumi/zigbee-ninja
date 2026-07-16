"""Offline decode of a coordinator↔Z2M capture: the spike-S1 command.

Usage:
    python -m zigbee_ninja.decode.pcap_cli capture.pcap --port 6638

Feeds each connection's two directions through the ASH decoder, runs EZSP
envelope parsing over DATA payloads in packet-timestamp order (so the version
negotiation is seen before extended frames), and prints per-connection ASH and
EZSP accounting. Exit code 0 when at least one connection decoded cleanly.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .ash import AshDecoder
from .ezsp import EzspStream
from .pcap import StreamBytes, read_tcp_segments


def analyze(data: bytes, port: int) -> dict:
    segments = [
        segment
        for segment in read_tcp_segments(data)
        if port in (segment.src[1], segment.dst[1])
    ]

    connections: dict[tuple, dict] = {}
    for segment in segments:
        to_coordinator = segment.dst[1] == port
        key = (segment.src, segment.dst) if to_coordinator else (segment.dst, segment.src)
        conn = connections.setdefault(
            key,
            {
                "host": key[0],
                "coordinator": key[1],
                "to_coord": {"bytes": StreamBytes(), "ash": AshDecoder(), "consumed": 0},
                "from_coord": {"bytes": StreamBytes(), "ash": AshDecoder(), "consumed": 0},
                "ezsp": EzspStream(),
                "ezsp_names": Counter(),
                "commands": 0,
                "responses": 0,
                "first_ts": segment.ts,
                "last_ts": segment.ts,
            },
        )
        conn["last_ts"] = max(conn["last_ts"], segment.ts)
        conn["first_ts"] = min(conn["first_ts"], segment.ts)

        direction = conn["to_coord"] if to_coordinator else conn["from_coord"]
        direction["bytes"].add(segment)
        stream = direction["bytes"]
        fresh = bytes(stream.data[direction["consumed"] :])
        direction["consumed"] = len(stream.data)
        for frame in direction["ash"].feed(fresh):
            if frame.type == "data" and frame.crc_ok:
                ezsp_frame = conn["ezsp"].feed(frame.payload)
                if ezsp_frame is not None:
                    conn["ezsp_names"][ezsp_frame.name] += 1
                    if ezsp_frame.is_response:
                        conn["responses"] += 1
                    else:
                        conn["commands"] += 1

    report = {"port": port, "tcp_segments": len(segments), "connections": []}
    for conn in connections.values():
        summary = {
            "host": f"{conn['host'][0]}:{conn['host'][1]}",
            "coordinator": f"{conn['coordinator'][0]}:{conn['coordinator'][1]}",
            "duration_s": round(conn["last_ts"] - conn["first_ts"], 3),
            "protocol_version": conn["ezsp"].protocol_version,
            "ezsp_frames": dict(conn["ezsp_names"].most_common()),
            "commands": conn["commands"],
            "responses_and_callbacks": conn["responses"],
            "ezsp_parse_errors": conn["ezsp"].parse_errors,
        }
        for label in ("to_coord", "from_coord"):
            ash = conn[label]["ash"].stats
            stream = conn[label]["bytes"]
            summary[label] = {
                "ash_frames": dict(ash.frames),
                "crc_errors": ash.crc_errors,
                "retransmits": ash.retransmits,
                "cancelled": ash.cancelled,
                "tcp_gaps": stream.gaps,
                "tcp_gap_bytes": stream.gap_bytes,
                "duplicate_bytes": stream.duplicate_bytes,
            }
        report["connections"].append(summary)
    return report


def _print_report(report: dict) -> None:
    print(f"port {report['port']}: {report['tcp_segments']} TCP segments, "
          f"{len(report['connections'])} connection(s)")
    for conn in report["connections"]:
        print(f"\n=== {conn['host']}  <->  {conn['coordinator']} "
              f"({conn['duration_s']}s, EZSP v{conn['protocol_version']})")
        print(f"  commands: {conn['commands']}  responses/callbacks: "
              f"{conn['responses_and_callbacks']}  parse errors: {conn['ezsp_parse_errors']}")
        for label in ("to_coord", "from_coord"):
            direction = conn[label]
            print(f"  {label}: ash={direction['ash_frames']} crc_err={direction['crc_errors']} "
                  f"reTx={direction['retransmits']} gaps={direction['tcp_gaps']}")
        print("  ezsp frames:")
        for name, count in conn["ezsp_frames"].items():
            print(f"    {count:>7}  {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--port", type=int, default=6638,
                        help="coordinator TCP port (default 6638)")
    args = parser.parse_args(argv)

    report = analyze(args.pcap.read_bytes(), args.port)
    _print_report(report)

    decoded_any = any(
        conn["to_coord"]["ash_frames"] or conn["from_coord"]["ash_frames"]
        for conn in report["connections"]
    )
    return 0 if decoded_any else 1


if __name__ == "__main__":
    sys.exit(main())
