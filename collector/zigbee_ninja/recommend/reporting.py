"""Reporting-configuration advisor (V2_PROPOSAL.md §V2-5 detector 3).

Autonomous reporting is priced per device in the daily ledger; on many
installations it is the single largest recoverable cost. Two comparisons
find the outliers:

- **Against hardware peers**: a device reporting far more than the median
  of other devices with the same vendor and model is one misconfigured
  instance of known-quiet hardware; that baseline is strong, so the
  finding is high confidence.
- **Against the fleet**: a device dominating the whole installation's
  reporting median gets a medium-confidence finding, downgraded to low
  when its name or model suggests presence sensing (occupancy streams may
  be deliberate; the owner decides).

Savings replay the recorded reporting volume at the reference rate: the
device's own recorded reports re-costed as if it reported like its peers.
"""

from __future__ import annotations

import json
import statistics

from ..capacity import airtime, ledger
from . import significance
from .context import DetectorContext
from .cost import KIND_STALENESS, STALENESS
from .store import Finding

NAME = "reporting"

PEER_RATIO = 3.0
MIN_PEERS = 3
FLEET_RATIO = 5.0
SAVING_FLOOR_US_PER_S = 50.0
# Names or models carrying these read as deliberate high-rate sensing.
PRESENCE_HINTS = ("presence", "occupancy", "motion", "mmwave", "radar", "pir")
# The denominator this advisor spends on is imported from `cost` (named for
# the direction it moves in: applying the action raises it). Not one
# significance.py knows about: nothing measures it, so it is reported as a
# quantity rather than banded.


def _recording_since(conn) -> float | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'ledger_since'"
    ).fetchone()
    return float(json.loads(row["value"])) if row else None


def _presence_like(name: str, model: str) -> bool:
    haystack = f"{name} {model}".lower()
    return any(hint in haystack for hint in PRESENCE_HINTS)


def _staleness_cost(
    *, per_day: float, rate: float, reference: float, presence: bool
) -> dict:
    """What slowing a device's reports costs on the denominator it does not
    save on.

    This action is not free just because it removes traffic. Nothing else on
    the mesh rises (a report never sent costs no air and no pipeline command),
    but every consumer of that device's state learns of a change later, and
    that delay is the whole price. The reference rate the saving is priced
    against implies the interval the device would move toward, so the cost is
    quotable in the currency the owner actually feels: seconds.

    Both intervals are means over the recorded window, not guarantees: a
    device reporting on change does not space its reports evenly.
    """
    now_interval = 86400.0 / per_day if per_day > 0 else None
    at_reference_per_day = per_day * (reference / rate) if rate > 0 else 0.0
    at_reference_interval = (
        86400.0 / at_reference_per_day if at_reference_per_day > 0 else None
    )
    if now_interval is not None and at_reference_interval is not None:
        added_delay = at_reference_interval - now_interval
        note = (
            f"reporting like its reference means about one report every "
            f"{at_reference_interval:.0f} s instead of every {now_interval:.1f} s, "
            f"so anything reacting to this device's state learns of a change up "
            f"to {added_delay:.0f} s later. Nothing on the airtime or "
            f"command-rate denominators rises: the reports simply stop being sent."
        )
    else:
        added_delay = None
        note = (
            "the devices this one is compared against report essentially never, "
            "so there is no reference interval to quote; the delay this adds is "
            "whatever minimum interval gets configured. Nothing on the airtime "
            "or command-rate denominators rises."
        )
    if presence:
        note += (
            " Presence hardware is where that delay is most likely to be felt, "
            "so pick the new interval against what reacts to it."
        )
    return {
        "kind": KIND_STALENESS,
        "denominator": STALENESS,
        "raises_load": False,
        "reports_per_day_now": int(per_day),
        "reports_per_day_at_reference": int(at_reference_per_day),
        "mean_interval_s_now": None if now_interval is None else round(now_interval, 1),
        "mean_interval_s_at_reference": (
            None if at_reference_interval is None else round(at_reference_interval, 1)
        ),
        "added_delay_s": None if added_delay is None else round(added_delay, 1),
        "presence_hardware": presence,
        "note": note,
    }


def detect(ctx: DetectorContext) -> list[Finding]:
    conn = ctx.conn
    recording_since = _recording_since(conn)
    if recording_since is None:
        return []
    effective_seconds = min(ctx.lookback_seconds, max(1.0, ctx.now - recording_since))

    days: list[str] = []
    t = ctx.window_start()
    while t <= ctx.now:
        day = ledger.utc_day(t)
        if day not in days:
            days.append(day)
        t += 86400.0
    last_day = ledger.utc_day(ctx.now)
    if last_day not in days:
        days.append(last_day)
    placeholders = ",".join("?" * len(days))

    spend: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
        f"SELECT instance, device, SUM(publishes) AS publishes, "
        f"SUM(autonomous_us) AS us FROM ledger_device_daily "
        f"WHERE day IN ({placeholders}) GROUP BY instance, device",
        days,
    ):
        spend[(row["instance"], row["device"])] = {
            "publishes": row["publishes"],
            "us_per_s": row["us"] / effective_seconds,
        }
    if not spend:
        return []

    # Registry join: hardware identity per device name, fleet-wide. The
    # registry stores vendor/model flattened onto the device dict (not the
    # raw Z2M nested `definition`). Devices with unknown hardware never form
    # a peer group: "same hardware" requires knowing the hardware.
    models: dict[tuple[str, str], tuple[str, str]] = {}
    peers_by_model: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for instance in ctx.instances:
        for device in ctx.devices(instance):
            name = device.get("friendly_name")
            vendor = device.get("vendor")
            model = device.get("model")
            if name and vendor and model:
                key = (vendor, model)
                models[(instance, name)] = key
                peers_by_model.setdefault(key, []).append((instance, name))

    fleet_rates = [entry["us_per_s"] for entry in spend.values() if entry["us_per_s"] > 0]
    fleet_median = statistics.median(fleet_rates) if fleet_rates else 0.0

    findings: list[Finding] = []
    for (instance, device), entry in spend.items():
        rate = entry["us_per_s"]
        if rate < SAVING_FLOOR_US_PER_S:
            continue
        model_key = models.get((instance, device))
        reference = None
        comparison = None
        if model_key is not None:
            peers = [
                peer for peer in peers_by_model.get(model_key, [])
                if peer != (instance, device)
            ]
            if len(peers) >= MIN_PEERS:
                peer_rates = [spend.get(peer, {}).get("us_per_s", 0.0) for peer in peers]
                peer_median = statistics.median(peer_rates)
                if rate >= max(PEER_RATIO * peer_median, SAVING_FLOOR_US_PER_S):
                    reference = peer_median
                    comparison = {
                        "compared_to": "peers",
                        "model": " ".join(model_key),
                        "peers": len(peers),
                        "peer_median_us_per_s": round(peer_median, 1),
                    }
        if comparison is None:
            if fleet_median <= 0 or rate < FLEET_RATIO * fleet_median:
                continue
            reference = fleet_median
            comparison = {
                "compared_to": "fleet",
                "devices": len(fleet_rates),
                "fleet_median_us_per_s": round(fleet_median, 1),
            }

        saved_us_per_s = rate - (reference or 0.0)
        if saved_us_per_s < SAVING_FLOOR_US_PER_S:
            continue
        # Hardware identity feeds the presence check even when the finding
        # itself fell through to the fleet comparison.
        presence = _presence_like(device, " ".join(model_key or ()))
        per_day = entry["publishes"] / effective_seconds * 86400.0
        if comparison["compared_to"] == "peers":
            confidence = "medium" if presence else "high"
            versus = (
                f"{comparison['peers']} other {comparison['model']} devices report "
                f"a median of {comparison['peer_median_us_per_s']:.0f} µs/s"
            )
        else:
            confidence = "low" if presence else "medium"
            versus = (
                f"the installation's median reporting device costs about "
                f"{comparison['fleet_median_us_per_s']:.0f} µs/s"
            )
        sentences = [
            f"{device} published about {per_day:.0f} reports per day "
            f"({rate:.0f} µs/s of airtime); {versus}.",
            "Raising its reporting minimum interval or change threshold would "
            "recover most of that.",
        ]
        if presence:
            sentences.append(
                "Its name or model suggests presence sensing, where a fast report "
                "stream may be deliberate."
            )
        saving = {
            "us_per_s": round(saved_us_per_s, 1),
            "pct_of_budget": round(
                saved_us_per_s / airtime.CHANNEL_BUDGET_US_PER_S * 100.0, 4
            ),
            "basis": (
                f"replayed {entry['publishes']} recorded reports against the "
                f"{comparison['compared_to']} median rate"
            ),
            "provenance": "modeled",
        }
        findings.append(
            Finding(
                detector=NAME,
                instance=instance,
                subject=device,
                finding=" ".join(sentences),
                action={
                    "kind": "reconfigure_reporting",
                    "instance": instance,
                    "device": device,
                    "reports_per_day": int(per_day),
                    "suggestion": "raise the reporting minimum interval or delta",
                },
                saving=saving,
                significance=significance.for_airtime(
                    saving, (ctx.utilization or {}).get(instance)
                ),
                cost=_staleness_cost(
                    per_day=per_day,
                    rate=rate,
                    reference=reference or 0.0,
                    presence=presence,
                ),
                confidence=confidence,
                evidence=[
                    {
                        "kind": "ledger",
                        "instance": instance,
                        "days": days,
                        "publishes": entry["publishes"],
                        "us_per_s": round(rate, 1),
                        **comparison,
                    }
                ],
                fingerprint={
                    "us_per_s": round(rate, 1),
                    "reports_per_day": int(per_day),
                },
            )
        )
    return findings
