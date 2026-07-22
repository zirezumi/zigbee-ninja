from zigbee_ninja.recommend import significance


def test_idle_denominator_bands_low_however_large_the_saving():
    # The case that motivated the term: the finding would remove nearly all of
    # what this mesh spends on airtime, but almost nothing is being spent, so
    # there is nothing to relieve. Relief near 100% and band low is the honest
    # reading of that, not a contradiction.
    result = significance.assess(saving_pct=0.47, utilization_pct=0.51)
    assert result["band"] == significance.BAND_LOW
    assert result["relief_pct"] > 90
    assert "not under pressure" in result["rationale"]


def test_contended_denominator_with_strong_relief_bands_high():
    result = significance.assess(saving_pct=9.0, utilization_pct=80.0)
    assert result["band"] == significance.BAND_HIGH
    assert "80.0% used" in result["rationale"]


def test_contended_denominator_with_weak_relief_bands_moderate():
    result = significance.assess(saving_pct=1.0, utilization_pct=80.0)
    assert result["band"] == significance.BAND_MODERATE


def test_pressure_floor_is_the_dominating_rule():
    # Just under the floor stays low; just over it can rise.
    below = significance.assess(
        saving_pct=50.0, utilization_pct=significance.PRESSURE_FLOOR_PCT - 0.1
    )
    above = significance.assess(
        saving_pct=50.0, utilization_pct=significance.PRESSURE_FLOOR_PCT + 0.1
    )
    assert below["band"] == significance.BAND_LOW
    assert above["band"] == significance.BAND_HIGH


def test_unmeasured_utilization_is_unknown_not_assumed_idle():
    # An installation that has never calibrated must not have its real
    # findings silently demoted.
    result = significance.assess(saving_pct=5.0, utilization_pct=None)
    assert result["band"] == significance.BAND_UNKNOWN
    assert "not measured yet" in result["rationale"]


def test_for_airtime_reads_the_channel_budget_utilization():
    result = significance.for_airtime(
        {"pct_of_budget": 0.47}, {"channel_budget_pct": 0.51}
    )
    assert result["band"] == significance.BAND_LOW
    assert result["denominator"] == significance.CHANNEL_AIRTIME


def test_band_rank_orders_pressure_above_size():
    # A small saving on a pressured mesh must outrank a large one on an idle
    # mesh; that inversion is the whole point of the term. Queue ordering
    # itself is store._rank's job and is tested there; this pins the band
    # ordering it reads.
    rank = significance.BAND_RANK
    assert rank[significance.BAND_HIGH] > rank[significance.BAND_MODERATE]
    assert rank[significance.BAND_MODERATE] > rank[significance.BAND_UNKNOWN]
    # Unmeasured outranks known-idle: never demote what has not been assessed.
    assert rank[significance.BAND_UNKNOWN] > rank[significance.BAND_LOW]
