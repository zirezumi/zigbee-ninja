from zigbee_ninja.capacity import airtime


def test_frame_airtime_sifs_lifs_boundary():
    # PSDU ≤ 18 bytes takes SIFS, larger takes LIFS (aMaxSIFSFrameSize).
    assert airtime.frame_airtime_us(18) == (6 + 18) * 32 + 192
    assert airtime.frame_airtime_us(19) == (6 + 19) * 32 + 640


def test_unicast_airtime_hand_computed():
    # payload 8 → PSDU 45+8=53 → (6+53)*32 = 1888 + LIFS 640 + ACK 352 = 2880.
    assert airtime.unicast_airtime_us(8) == 2880.0


def test_groupcast_amplification():
    # payload 8 → PSDU 54 → frame (6+54)*32=1920 + LIFS 640 = 2560 per TX,
    # amplified by (1 + 10 routers) × avg_tx 1.3.
    assert airtime.groupcast_airtime_us(8, n_routers=10) == 2560 * 11 * 1.3
    # A router-less mesh still costs the coordinator's own transmission.
    assert airtime.groupcast_airtime_us(8, n_routers=0) == 2560 * 1.3
    assert airtime.groupcast_airtime_us(8, n_routers=-1) == 2560 * 1.3


def test_incoming_airtime_addressing_and_ack():
    acked = airtime.incoming_airtime_us(5, group_addressed=False, acked=True)
    unacked = airtime.incoming_airtime_us(5, group_addressed=False, acked=False)
    assert acked - unacked == airtime.ACK_AIRTIME_US
    group = airtime.incoming_airtime_us(5, group_addressed=True, acked=False)
    assert group - unacked == airtime.US_PER_BYTE  # one extra APS header byte


def test_mesh_maintenance_frames():
    # Route record with no relays: PSDU 37+2=39 → 1440 + 640 + 352 = 2432.
    assert airtime.route_record_airtime_us(0) == 2432.0
    assert airtime.route_record_airtime_us(2) - airtime.route_record_airtime_us(0) == 4 * 32
    # NWK status: PSDU 41 → 1504 + 640 + 352 = 2496.
    assert airtime.network_status_airtime_us() == 2496.0


def test_channel_budget_constant():
    assert airtime.CHANNEL_BUDGET_US_PER_S == 700_000.0
