from zigbee_ninja.capacity import airtime, ledger


def test_group_chain_uses_amplification_and_measured_avg_tx():
    price = ledger.price_chain(
        verb="set", group_target=True, n_routers=20, echo_count=15, avg_tx=2.1
    )
    assert price.tx_us == airtime.groupcast_airtime_us(
        ledger.ZCL_SET_BYTES, 20, avg_tx=2.1
    )
    assert price.rx_us == 15 * ledger.autonomous_publish_cost_us()
    assert price.total_us == price.tx_us + price.rx_us
    assert price.provenance.startswith("inferred")
    assert price.params["avg_tx"] == 2.1
    assert price.params["retry_rate"] is None


def test_device_chain_uses_unicast_and_retry_rate():
    price = ledger.price_chain(
        verb="set", group_target=False, n_routers=20, echo_count=1, retry_rate=0.08
    )
    assert price.tx_us == airtime.unicast_airtime_us(ledger.ZCL_SET_BYTES, retry_rate=0.08)
    assert price.params["n_routers"] == 0
    assert price.params["retry_rate"] == 0.08


def test_get_prices_smaller_than_set_and_defaults_apply():
    get_price = ledger.price_chain(
        verb="get", group_target=False, n_routers=0, echo_count=0
    )
    set_price = ledger.price_chain(
        verb="set", group_target=False, n_routers=0, echo_count=0
    )
    assert get_price.tx_us < set_price.tx_us
    assert get_price.rx_us == 0
    assert get_price.params["retry_rate"] == 0.0
