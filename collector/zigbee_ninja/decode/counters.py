"""EmberZNet stack-counter labeling for passively harvested counter reads.

Zigbee2MQTT itself issues readAndClearCounters against its coordinators; the
wire tap sees the responses for free, which is the passive answer to spike S2
(DESIGN.md §4, §19): no zigbee-ninja polling, no added NCP work.

The name table below is written from the Silicon Labs UG100 / Zigbee stack API
documentation's counter-type enumeration (clean room — DESIGN.md §16). The
enum order has been stable across EmberZNet 6.x–8.x for the leading entries,
but exact tail composition varies by SDK build, so treatment is defensive:

- indices beyond the table label as ``counter_NN`` rather than guessing;
- labels carry provenance ``inferred`` until validated against live behavior
  (e.g. mac_tx_broadcast tracking the ~15 s link-status cadence, and
  mac_tx_unicast_success tracking tap-observed sendUnicast volume between
  polls).

Of particular capacity-model interest (§10): ``mac_tx_unicast_retry`` /
``mac_tx_unicast_success`` expose the per-hop MAC retry rate that no passive
tier can otherwise see, and ``phy_cca_fail_count`` is a direct channel
contention signal.
"""

from __future__ import annotations

COUNTER_NAMES = [
    "mac_rx_broadcast",
    "mac_tx_broadcast",
    "mac_rx_unicast",
    "mac_tx_unicast_success",
    "mac_tx_unicast_retry",
    "mac_tx_unicast_failed",
    "aps_data_rx_broadcast",
    "aps_data_tx_broadcast",
    "aps_data_rx_unicast",
    "aps_data_tx_unicast_success",
    "aps_data_tx_unicast_retry",
    "aps_data_tx_unicast_failed",
    "route_discovery_initiated",
    "neighbor_added",
    "neighbor_removed",
    "neighbor_stale",
    "join_indication",
    "child_removed",
    "ash_overflow_error",
    "ash_framing_error",
    "ash_overrun_error",
    "nwk_frame_counter_failure",
    "aps_frame_counter_failure",
    "utility",
    "aps_link_key_not_authorized",
    "nwk_decryption_failure",
    "aps_decryption_failure",
    "allocate_packet_buffer_failure",
    "relayed_unicast",
    "phy_to_mac_queue_limit_reached",
    "packet_validate_library_dropped_count",
    "nwk_retry_overflow",
    "phy_cca_fail_count",
    "broadcast_table_full",
    "pta_lo_pri_requested",
    "pta_hi_pri_requested",
    "pta_lo_pri_denied",
    "pta_hi_pri_denied",
    "pta_lo_pri_tx_aborted",
    "pta_hi_pri_tx_aborted",
]

PROVENANCE = "inferred"  # until live cross-tier validation promotes the map


def counter_name(index: int) -> str:
    if 0 <= index < len(COUNTER_NAMES):
        return COUNTER_NAMES[index]
    return f"counter_{index:02d}"


def label_counters(values: list[int], *, include_zero: bool = False) -> dict[str, int]:
    """Label a raw counter array; zero-valued counters are dropped by default
    (the arrays are wide and mostly zero on a healthy mesh)."""
    return {
        counter_name(index): value
        for index, value in enumerate(values)
        if include_zero or value
    }
