from zigbee_ninja.capacity import airtime, hops


def _graph():
    """C(coordinator) - A - B, plus D partitioned from the rest."""
    return {
        "nodes": [
            {"id": "0xC", "name": "Coordinator", "type": "Coordinator"},
            {"id": "0xA", "name": "near_light", "type": "Router"},
            {"id": "0xB", "name": "far_light", "type": "Router"},
            {"id": "0xD", "name": "orphan_light", "type": "Router"},
        ],
        "links": [
            {"source": "0xC", "target": "0xA", "lqi": 200},
            {"source": "0xA", "target": "0xB", "lqi": 150},
        ],
    }


def test_depths_count_hops_outward_from_the_coordinator():
    assert hops.depths_by_ieee(_graph()) == {"0xC": 0, "0xA": 1, "0xB": 2}


def test_partitioned_node_gets_no_depth():
    # Absent rather than guessed: the caller prices it at the §10 default and
    # tags the provenance, instead of pretending the map placed it.
    assert "0xD" not in hops.depths_by_ieee(_graph())


def test_registry_coordinator_wins_over_node_type():
    graph = _graph()
    # Map mislabels the type; the registry's authoritative IEEE still roots it.
    graph["nodes"][0]["type"] = "Router"
    graph["nodes"][1]["type"] = "Coordinator"
    assert hops.depths_by_ieee(graph, "0xC")["0xB"] == 2


def test_unknown_coordinator_ieee_falls_back_to_node_type():
    assert hops.depths_by_ieee(_graph(), "0xNOTHERE")["0xB"] == 2


def test_depths_by_name_drops_the_coordinator():
    by_name = hops.depths_by_name(_graph())
    assert by_name == {"near_light": 1, "far_light": 2}
    assert "Coordinator" not in by_name


def test_graph_without_a_coordinator_places_nothing():
    graph = {"nodes": [{"id": "0xA", "name": "a", "type": "Router"}], "links": []}
    assert hops.depths_by_ieee(graph) == {}
    assert hops.depths_by_name(graph) == {}


def test_unicast_airtime_scales_with_hop_count():
    one = airtime.unicast_airtime_us(10, hops=1)
    three = airtime.unicast_airtime_us(10, hops=3)
    assert three == one * 3


def test_unicast_airtime_defaults_to_the_coordinator_hop():
    # Callers that know nothing about the route keep the pre-hop behaviour.
    assert airtime.unicast_airtime_us(10) == airtime.unicast_airtime_us(10, hops=1)


def test_unicast_hop_and_retry_terms_compose():
    assert airtime.unicast_airtime_us(10, retry_rate=0.5, hops=2) == (
        airtime.unicast_airtime_us(10) * 2 * 1.5
    )
