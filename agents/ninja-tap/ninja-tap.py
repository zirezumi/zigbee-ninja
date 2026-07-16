#!/usr/bin/env python3
"""ninja-tap — passive wire-tap capture agent (DESIGN.md §7.2).

Dumb agent, smart collector: shells out to tcpdump for the coordinator TCP
flows discovery found, and streams raw pcap-record frames to the collector over
an outbound, token-authenticated WebSocket. Knows nothing about Zigbee — all
reassembly and ASH/EZSP decode happen collector-side.

Standard library only (no pip install on capture hosts). tcpdump is the sole
external dependency and is invoked, never linked (keeps the agent BSD-clean).

  ninja-tap.py --collector ws://HOST:8686/api/ws/tap \
               --token TOKEN --filter "tcp port 6638" [--iface vmbr0]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time

RECONNECT_BACKOFF = 2  # seconds between capture sessions


def log(msg: str) -> None:
    print(f"[ninja-tap] {msg}", file=sys.stderr, flush=True)


def pick_iface(sample_host: str) -> str:
    """Interface with the route to a coordinator (best-effort default)."""
    try:
        fields = subprocess.check_output(
            ["ip", "-o", "route", "get", sample_host], text=True
        ).split()
        if "dev" in fields:
            return fields[fields.index("dev") + 1]
    except Exception:
        pass
    return "any"


class Collector:
    """Length-prefixed framed sender over a raw WebSocket (RFC6455 client).

    A tiny hand-rolled client keeps the agent stdlib-only. Frames are binary;
    the first message is a JSON hello with the agent token and capture metadata.
    """

    def __init__(self, url: str, token: str, meta: dict):
        self._url = url
        self._token = token
        self._meta = meta
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(self._url)
        host, port = parsed.hostname, parsed.port or 80
        path = parsed.path or "/"
        key = base64.b64encode(os.urandom(16)).decode()
        raw = socket.create_connection((host, port), timeout=10)
        handshake = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self._token}\r\n\r\n"
        )
        raw.sendall(handshake.encode())
        resp = raw.recv(4096)
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket upgrade failed: {resp[:80]!r}")
        self._sock = raw
        self._send_text(json.dumps({"type": "hello", **self._meta}))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self._sock is not None
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def send_binary(self, data: bytes) -> None:
        self._send_frame(0x2, data)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._send_frame(0x8, b"")
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def stream(args: argparse.Namespace) -> None:
    """One capture session: connect, THEN start tcpdump so this collector

    connection receives a fresh pcap global header, and stream until either
    side drops. A disconnect returns so the outer loop restarts the whole
    session (fresh header again) — the collector's per-connection pcap reader
    never sees headerless mid-stream bytes.
    """
    iface = args.iface or pick_iface(args.sample_host)
    meta = {
        "agent": args.name,
        "iface": iface,
        "filter": args.filter,
        "pcap_snaplen": args.snaplen,
        "started_at": time.time(),
    }

    collector = Collector(args.collector, args.token, meta)
    collector.connect()  # OSError here bubbles to main()'s backoff
    log("connected to collector")

    # -Z root: do not drop privileges / chroot. The agent already runs confined
    # to CAP_NET_RAW; tcpdump's default drop-and-chroot to /var/lib/tcpdump fails
    # (and exits) under a hardened systemd sandbox (ProtectSystem/NoNewPrivileges),
    # which otherwise shows up as tcpdump exiting immediately in a tight loop.
    tcpdump = subprocess.Popen(
        ["tcpdump", "-i", iface, "-w", "-", "-U", "-Z", "root",
         "-s", str(args.snaplen), args.filter],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    log(f"capturing on {iface}: {args.filter}")
    assert tcpdump.stdout is not None
    try:
        while True:
            # read1 forwards whatever tcpdump has written (-U flushes per
            # packet) instead of blocking until a full block accumulates —
            # read(n) would hold quiet flows' frames for tens of seconds,
            # which downstream consumers keyed on pcap timestamps never
            # noticed but arrival-time fusion does.
            chunk = tcpdump.stdout.read1(65536)
            if not chunk:
                return
            try:
                collector.send_binary(chunk)
            except OSError as exc:
                log(f"collector dropped ({exc}); restarting capture")
                return
    finally:
        tcpdump.terminate()
        collector.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector", required=True, help="ws://host:port/api/ws/tap")
    parser.add_argument("--token", help="agent token (prefer --token-file for systemd)")
    parser.add_argument("--token-file", help="path to a file containing the agent token")
    parser.add_argument("--filter", default="tcp port 6638", help="BPF filter")
    parser.add_argument("--iface", default=None, help="capture interface (auto if omitted)")
    parser.add_argument("--sample-host", default="192.168.1.71",
                        help="a coordinator IP, used only to auto-pick the interface")
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--snaplen", type=int, default=2048)
    args = parser.parse_args()

    if args.token_file:
        with open(args.token_file) as fh:
            args.token = fh.read().strip()
    if not args.token:
        parser.error("one of --token or --token-file is required")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            stream(args)
        except KeyboardInterrupt:
            return
        except Exception as exc:  # noqa: BLE001 - agent must survive tcpdump/collector drops
            log(f"session error ({exc})")
        time.sleep(RECONNECT_BACKOFF)


if __name__ == "__main__":
    main()
