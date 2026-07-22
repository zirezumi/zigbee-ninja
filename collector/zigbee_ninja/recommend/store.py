"""Recommendation rows + lifecycle (V2_PROPOSAL.md §V2-5).

Detectors emit findings; the store reconciles them with persisted rows so
the queue stays stable across runs:

- a finding new to the store inserts as ``open``;
- a finding matching an ``open`` row refreshes that row in place (same id,
  same created_at, so the queue does not churn);
- an ``open`` row its detector no longer emits is deleted: open rows are
  detector output, and a finding the evidence no longer supports must leave
  the queue (an empty queue is the product's "traffic-optimized" claim);
- a ``dismissed`` row stays dismissed until its input fingerprint changes
  materially (a numeric input moving by MATERIAL_RATIO, or a structural
  change), then reopens with a note. Dismissals are durable against
  re-detection, not against a genuinely different situation;
- ``applied`` / ``verified`` / ``regressed`` rows are never touched by
  detector runs: §V2-6 verification (V2.M4) owns those transitions.

The row's JSON fields serve the §V2-5 recommendation shape verbatim; that
shape is a frozen contract (§V2-10.3), so fields are only ever added.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..store.db import Database
from . import significance

STATES = ("open", "dismissed", "applied", "verified", "regressed")
# States a user may set through the API; verified and regressed are §V2-6
# verification verdicts, written only by the verification pass.
SETTABLE_STATES = ("open", "dismissed", "applied")
ALLOWED_TRANSITIONS = {
    ("open", "dismissed"),
    ("open", "applied"),
    ("dismissed", "open"),
    ("applied", "open"),
    # A verdict-bearing row may be taken up again or put to rest by the
    # user; its verification receipts stay attached either way.
    ("regressed", "open"),
    ("regressed", "dismissed"),
    ("verified", "open"),
}

# A dismissed finding reopens only when an input moves by at least this
# factor: re-detection at the same magnitude never nags (§V2-5 principles).
MATERIAL_RATIO = 1.5

CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}


@dataclass
class Finding:
    """One detector result; the store keys it by (detector, instance, subject)."""

    detector: str
    instance: str
    subject: str
    finding: str
    action: dict
    saving: dict
    confidence: str  # high | medium | low
    evidence: list = field(default_factory=list)
    fingerprint: dict = field(default_factory=dict)
    # What the change would free, weighed against how contended that resource
    # actually is (see significance.py). Empty when a detector has not been
    # taught to assess it.
    significance: dict = field(default_factory=dict)
    # What the change would COST on the denominators it does not save on. A
    # detector that raises load somewhere must say so here: the queue is not
    # allowed to recommend spending a scarce resource to save an abundant one.
    cost: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return finding_id(self.detector, self.instance, self.subject)


def finding_id(detector: str, instance: str, subject: str) -> str:
    """Stable id per finding identity: re-detection maps onto the same row,
    which is what makes dismissals durable."""
    digest = hashlib.sha1(f"{detector}|{instance}|{subject}".encode()).hexdigest()
    return f"rec-{digest[:12]}"


def materially_changed(old: dict, new: dict) -> bool:
    """Whether a finding's inputs moved enough to reopen a dismissal.

    Numeric fields compare as a ratio against MATERIAL_RATIO (sign flips and
    zero crossings count); everything else compares by equality; a key
    appearing or disappearing is structural and always material.
    """
    if set(old) != set(new):
        return True
    for key, old_value in old.items():
        new_value = new[key]
        numeric = isinstance(old_value, (int, float)) and isinstance(new_value, (int, float))
        if numeric and not isinstance(old_value, bool) and not isinstance(new_value, bool):
            if old_value == new_value:
                continue
            if (old_value >= 0) != (new_value >= 0):
                return True
            low, high = sorted((abs(old_value), abs(new_value)))
            if low == 0 or high / low >= MATERIAL_RATIO:
                return True
        elif old_value != new_value:
            return True
    return False


def _rank(row: dict) -> tuple[float, float, float, float]:
    """Queue order: significance band, then §V2-5's saving × confidence.

    The band leads because `saving × confidence` alone ranks a large saving on
    an idle mesh above a small one on a saturated mesh, which is backwards: it
    measures how much a change frees without asking whether anything needed
    freeing. Within a band the original ordering is preserved exactly. Airtime
    savings rank first; latency-only findings (µs/s of zero) rank among
    themselves by their predicted p95 improvement at the same weighting.
    """
    weight = CONFIDENCE_WEIGHT.get(row["confidence"], 0.3)
    saving = row["saving"] or {}
    band = significance.BAND_RANK.get(
        (row.get("significance") or {}).get("band"),
        significance.BAND_RANK[significance.BAND_UNKNOWN],
    )
    return (
        float(band),
        weight * float(saving.get("us_per_s") or 0.0),
        weight * float(saving.get("p95_ms") or 0.0),
        row["updated_at"],
    )


class RecommendationStore:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self._db = db
        self._clock = clock

    # -- detector reconciliation ---------------------------------------------------

    def sync(self, detector: str, findings: list[Finding]) -> dict:
        """Reconcile one detector's current output with its persisted rows.

        Only called for a detector run that completed: a crashed detector
        must not delete the open rows its last good run produced.
        """
        now = self._clock()
        conn = self._db.connect()
        existing = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM recommendations WHERE detector = ?", (detector,)
            )
        }
        counts = {"inserted": 0, "updated": 0, "reopened": 0, "deleted": 0, "held": 0}
        seen: set[str] = set()
        for item in findings:
            rec_id = item.id
            seen.add(rec_id)
            row = existing.get(rec_id)
            payload = (
                item.finding,
                json.dumps(item.action),
                json.dumps(item.saving),
                item.confidence,
                json.dumps(item.evidence),
                json.dumps(item.fingerprint),
                json.dumps(item.significance),
                json.dumps(item.cost),
                now,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO recommendations (id, detector, instance, subject, "
                    "finding, action, saving, confidence, evidence, state, "
                    "fingerprint, significance, cost, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                    (
                        rec_id,
                        item.detector,
                        item.instance,
                        item.subject,
                        item.finding,
                        json.dumps(item.action),
                        json.dumps(item.saving),
                        item.confidence,
                        json.dumps(item.evidence),
                        json.dumps(item.fingerprint),
                        json.dumps(item.significance),
                        json.dumps(item.cost),
                        now,
                        now,
                    ),
                )
                counts["inserted"] += 1
            elif row["state"] == "open":
                conn.execute(
                    "UPDATE recommendations SET finding = ?, action = ?, saving = ?, "
                    "confidence = ?, evidence = ?, fingerprint = ?, significance = ?, "
                    "cost = ?, updated_at = ? "
                    "WHERE id = ?",
                    (*payload, rec_id),
                )
                counts["updated"] += 1
            elif row["state"] == "dismissed":
                old_fingerprint = json.loads(row["fingerprint"] or "{}")
                if materially_changed(old_fingerprint, item.fingerprint):
                    conn.execute(
                        "UPDATE recommendations SET finding = ?, action = ?, saving = ?, "
                        "confidence = ?, evidence = ?, fingerprint = ?, significance = ?, "
                        "cost = ?, updated_at = ?, "
                        "state = 'open', state_changed_at = ?, state_note = ? "
                        "WHERE id = ?",
                        (
                            *payload,
                            now,
                            "reopened: inputs changed materially since dismissal",
                            rec_id,
                        ),
                    )
                    counts["reopened"] += 1
                else:
                    counts["held"] += 1
            else:
                # applied / verified / regressed: verification territory.
                counts["held"] += 1
        stale_open = [
            rec_id
            for rec_id, row in existing.items()
            if row["state"] == "open" and rec_id not in seen
        ]
        if stale_open:
            conn.executemany(
                "DELETE FROM recommendations WHERE id = ?",
                [(rec_id,) for rec_id in stale_open],
            )
            counts["deleted"] = len(stale_open)
        conn.commit()
        return counts

    # -- lifecycle -------------------------------------------------------------------

    def set_state(self, rec_id: str, state: str, note: str | None = None) -> dict | None:
        """User-driven transition; returns the updated row, None if unknown id,
        raises ValueError on an invalid state or transition."""
        if state not in SETTABLE_STATES:
            raise ValueError(
                f"State must be one of {', '.join(SETTABLE_STATES)}; "
                "verified and regressed are verification verdicts"
            )
        conn = self._db.connect()
        row = conn.execute(
            "SELECT state FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()
        if row is None:
            return None
        if (row["state"], state) not in ALLOWED_TRANSITIONS:
            raise ValueError(f"Cannot move a {row['state']} recommendation to {state}")
        now = self._clock()
        conn.execute(
            "UPDATE recommendations SET state = ?, state_changed_at = ?, state_note = ? "
            "WHERE id = ?",
            (state, now, note, rec_id),
        )
        conn.commit()
        return self.get(rec_id)

    # -- verification (V2_PROPOSAL.md §V2-6) -----------------------------------------

    def applied_rows(self) -> list[dict]:
        """Applied rows awaiting a verification verdict."""
        conn = self._db.connect()
        return [
            self._serialize(row)
            for row in conn.execute(
                "SELECT * FROM recommendations WHERE state = 'applied'"
            )
        ]

    def open_rows(self, detector: str) -> list[dict]:
        conn = self._db.connect()
        return [
            self._serialize(row)
            for row in conn.execute(
                "SELECT * FROM recommendations WHERE state = 'open' AND detector = ?",
                (detector,),
            )
        ]

    def mark_applied_auto(self, rec_id: str, boundary: float, note: str) -> None:
        """Journal-detected application: the boundary is when the registry
        actually saw the change, not when the pass noticed it."""
        conn = self._db.connect()
        conn.execute(
            "UPDATE recommendations SET state = 'applied', state_changed_at = ?, "
            "state_note = ? WHERE id = ? AND state = 'open'",
            (boundary, note, rec_id),
        )
        conn.commit()

    def record_verification(self, rec_id: str, receipts: dict) -> None:
        """Attach in-progress verification receipts without a state change."""
        conn = self._db.connect()
        conn.execute(
            "UPDATE recommendations SET verification = ? WHERE id = ?",
            (json.dumps(receipts), rec_id),
        )
        conn.commit()

    def set_verdict(self, rec_id: str, verdict_state: str, note: str, receipts: dict) -> None:
        """Verification's own transition: applied to verified or regressed."""
        if verdict_state not in ("verified", "regressed"):
            raise ValueError(f"Not a verification verdict: {verdict_state}")
        conn = self._db.connect()
        conn.execute(
            "UPDATE recommendations SET state = ?, state_changed_at = ?, "
            "state_note = ?, verification = ? WHERE id = ? AND state = 'applied'",
            (verdict_state, self._clock(), note, json.dumps(receipts), rec_id),
        )
        conn.commit()

    # -- read side --------------------------------------------------------------------

    def _serialize(self, row) -> dict:
        return {
            "id": row["id"],
            "detector": row["detector"],
            "instance": row["instance"],
            "subject": row["subject"],
            "finding": row["finding"],
            "action": json.loads(row["action"] or "{}"),
            "saving": json.loads(row["saving"] or "{}"),
            "confidence": row["confidence"],
            "evidence": json.loads(row["evidence"] or "[]"),
            "significance": json.loads(row["significance"] or "{}"),
            "cost": json.loads(row["cost"] or "{}"),
            "state": row["state"],
            "state_note": row["state_note"],
            "verification": json.loads(row["verification"]) if row["verification"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "state_changed_at": row["state_changed_at"],
        }

    def get(self, rec_id: str) -> dict | None:
        row = self._db.connect().execute(
            "SELECT * FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()
        return self._serialize(row) if row else None

    def queue(self, state: str = "open") -> list[dict]:
        """Rows for the Recommendations view, ordered by saving × confidence."""
        conn = self._db.connect()
        if state == "all":
            rows = conn.execute("SELECT * FROM recommendations").fetchall()
        else:
            if state not in STATES:
                raise ValueError(f"Unknown state filter: {state}")
            rows = conn.execute(
                "SELECT * FROM recommendations WHERE state = ?", (state,)
            ).fetchall()
        serialized = [self._serialize(row) for row in rows]
        serialized.sort(key=_rank, reverse=True)
        return serialized

    def counts(self) -> dict:
        """State totals plus open counts per instance (the HA sensor's number)."""
        conn = self._db.connect()
        by_state = {
            row["state"]: row["n"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS n FROM recommendations GROUP BY state"
            )
        }
        open_by_instance = {
            row["instance"]: row["n"]
            for row in conn.execute(
                "SELECT instance, COUNT(*) AS n FROM recommendations "
                "WHERE state = 'open' GROUP BY instance"
            )
        }
        return {
            "by_state": {state: by_state.get(state, 0) for state in STATES},
            "open_by_instance": open_by_instance,
        }
