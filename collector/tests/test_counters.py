from zigbee_ninja.decode.counters import COUNTER_NAMES, counter_name, label_counters


def test_label_counters_drops_zeros_and_names_leading_indices():
    values = [0] * 40
    values[1] = 82  # mac_tx_broadcast
    values[3] = 812  # mac_tx_unicast_success
    values[4] = 31  # mac_tx_unicast_retry
    values[32] = 5  # phy_cca_fail_count
    labeled = label_counters(values)
    assert labeled == {
        "mac_tx_broadcast": 82,
        "mac_tx_unicast_success": 812,
        "mac_tx_unicast_retry": 31,
        "phy_cca_fail_count": 5,
    }


def test_unknown_tail_indices_degrade_to_numbered_labels():
    values = [0] * 44
    values[43] = 7
    assert label_counters(values) == {"counter_43": 7}
    assert counter_name(len(COUNTER_NAMES)) == f"counter_{len(COUNTER_NAMES)}"


def test_include_zero_returns_full_width():
    assert len(label_counters([0] * 40, include_zero=True)) == 40
