from zigbee_ninja.ingest.brokerlog import BrokerLogCorrelator


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


PUBLISH_LINE = (
    b"1720000000: Received PUBLISH from ha-core "
    b"(d0, q0, r0, m0, 'z2m-test/lamp/set', ... (17 bytes))"
)


def test_parses_publish_line():
    correlator = BrokerLogCorrelator(clock=FakeClock())
    parsed = correlator.on_log(PUBLISH_LINE)
    assert parsed == ("ha-core", "z2m-test/lamp/set")
    assert correlator.parsed == 1


def test_client_for_within_tolerance():
    clock = FakeClock()
    correlator = BrokerLogCorrelator(clock=clock)
    correlator.on_log(PUBLISH_LINE)
    clock.now += 1.0
    assert correlator.client_for("z2m-test/lamp/set") == "ha-core"
    clock.now += 5.0
    assert correlator.client_for("z2m-test/lamp/set") is None


def test_non_publish_lines_degrade_gracefully():
    correlator = BrokerLogCorrelator(clock=FakeClock())
    assert correlator.on_log(b"1720000000: New client connected from 10.0.0.9") is None
    assert correlator.on_log(b"\x00\xff garbage") is None
    assert correlator.unparsed == 2
    assert correlator.client_for("z2m-test/lamp/set") is None
