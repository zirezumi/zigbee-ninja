"""Migration manifest export (V2_PROPOSAL.md §V2-11, contract ratified).

The manifest is the boundary between zigbee-ninja and whatever tooling
actually moves devices: a versioned, self-auditing JSON document carrying
each move's subject identity, the predicted per-move delta with its
provenance and basis embedded, and the predicted per-instance after-state.
The embedded predictions are the §V2-6 verification receipts: when the
change journal later sees the moves happen, the measured after-window is
compared against exactly these numbers.

CONTRACT FREEZE: this shape shipped with MANIFEST_VERSION 1. Field renames
or removals from here on are breaking changes (§V2-10.3); fields may only
be added. zigbee-ninja never executes a manifest; applying it is the
user's tooling's job.
"""

from __future__ import annotations

import time
from collections.abc import Callable

MANIFEST_VERSION = 1
SOURCES = ("simulator", "advisor")

PREDICTION_NOTE = (
    "predictions reprice recorded traffic; comparable estimates, not meter "
    "readings, and the §V2-6 verification receipts for these moves"
)


def build(
    report: dict,
    registry,
    moves: list[dict],
    source: str,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Assemble the manifest from a priced scenario report.

    ``report`` is ``scenario.price_scenario``'s output for exactly
    ``moves``; the manifest embeds its per-move and per-instance numbers
    verbatim so the export can never disagree with what the simulator or
    advisor displayed.
    """
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, not {source!r}")

    reported = {
        (entry["kind"], entry["from_instance"], entry["subject"]): entry
        for entry in report["moves"]
    }
    splits_by_mover: dict[tuple[str, str], dict] = {}
    for split in report.get("splits", []):
        for mover in split["movers"]:
            splits_by_mover[(split["instance"], mover)] = split

    manifest_moves: list[dict] = []
    for move in moves:
        key = (move["kind"], move["from_instance"], move["subject"])
        entry = reported.get(key)
        if entry is None:
            raise ValueError(
                f"move {move['subject']!r} is not in the priced report; "
                "price and export must use the same move list"
            )
        split = splits_by_mover.get((move["from_instance"], move["subject"]))
        commands = entry["commands"]
        predicted = {
            "commands_before_us_per_s": commands["before_us_per_s"],
            "commands_after_us_per_s": commands["after_us_per_s"],
            "chains_per_s": commands["chains_per_s"],
            "reports_us_per_s": (entry.get("reports") or {}).get("us_per_s", 0.0),
            "provenance": commands["provenance"],
            "basis": PREDICTION_NOTE,
        }
        out: dict = {
            "kind": move["kind"],
            "subject": move["subject"],
            "from_instance": move["from_instance"],
            "to_instance": move["to_instance"],
            "predicted": predicted,
        }
        if move["kind"] == "device":
            out["ieee"] = entry.get("ieee")
            out["radio"] = entry.get("radio")
            if split is not None:
                out["group_resolution"] = split["applied_resolution"]
                out["group_split"] = {
                    "group": split["group"],
                    "stayers": split["stayers"],
                    "resolutions_us_per_s": split["added_us_per_s"],
                }
        else:
            ieee_by_name = {
                device["friendly_name"]: device.get("ieee_address")
                for device in registry.devices(move["from_instance"])
                if device.get("friendly_name")
            }
            out["members"] = [
                {"name": name, "ieee": ieee_by_name.get(name)}
                for name in entry.get("members", [])
            ]
        manifest_moves.append(out)

    predicted_instances = {
        base: {
            "steady_after_us_per_s": entry["steady"]["after_us_per_s"],
            "steady_after_pct_of_budget": entry["steady"]["after_pct_of_budget"],
            "burst_after_peak_1s_eps": (
                entry["burst"]["after_peak_1s"]["eps_1s"]
                if entry["burst"]["after_peak_1s"]
                else None
            ),
            "verdict": entry["burst"]["verdict"],
            "routers_after": entry["census"]["routers_after"],
            "limits": entry["limits"],
            "touched": entry["touched"],
        }
        for base, entry in report["instances"].items()
    }

    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": clock(),
        "source": source,
        "window_seconds": report["window_seconds"],
        "basis": {**report["basis"], "predictions": PREDICTION_NOTE},
        "moves": manifest_moves,
        "predicted_instances": predicted_instances,
    }
