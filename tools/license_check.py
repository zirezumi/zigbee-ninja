"""Fail if any installed dependency carries a GPL or AGPL license.

Policy: docs/DESIGN.md §16 — no GPL/AGPL code in distributed artifacts. LGPL is
intentionally not matched (dynamic linking is outside the ban); the named
always-banned packages are rejected by name regardless of reported license.

Run inside the environment that has the collector installed:
    python tools/license_check.py
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

# Matches GPL/AGPL but not LGPL ("Lesser"): \bGPL has no word boundary inside
# "LGPL", and the long-form alternative requires the exact GNU GPL phrasing.
DENY = re.compile(r"\b(?:AGPL|GPL)\b|GNU (?:Affero )?General Public License", re.IGNORECASE)
LESSER = re.compile(r"\bLGPL\b|Lesser General Public", re.IGNORECASE)

BANNED_NAMES = {"bellows", "zigpy", "scapy"}
SELF = {"zigbee-ninja"}


def license_fields(dist: metadata.Distribution) -> str:
    md = dist.metadata
    fields = [md.get("License") or ""]
    fields += [
        classifier.split("::")[-1].strip()
        for classifier in (md.get_all("Classifier") or [])
        if classifier.startswith("License")
    ]
    return "; ".join(field for field in fields if field)


def main() -> int:
    violations: list[tuple[str, str]] = []
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").lower()
        if not name or name in SELF:
            continue
        license_text = license_fields(dist)
        if name in BANNED_NAMES:
            violations.append((name, f"banned by name (policy §16); reports: {license_text}"))
        elif DENY.search(license_text) and not LESSER.search(license_text):
            violations.append((name, license_text))

    if violations:
        print("License policy violations (DESIGN.md §16):")
        for name, license_text in sorted(violations):
            print(f"  {name}: {license_text}")
        return 1

    print("License policy: OK (no GPL/AGPL dependencies found)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
