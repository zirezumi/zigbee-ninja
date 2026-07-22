"""LoopActivityLog must survive a collection landing inside its own lock.

`note()` allocates inside its critical section, any allocation can trigger a
garbage collection, and a collection fires `gc.callbacks` on the very thread
that is already holding the lock. With a non-reentrant lock that is a
permanent self-deadlock, and because it needs the collection to land inside a
narrow window it presents as an occasional wedge rather than a reliable
failure: on 2026-07-22 it took out the event loop on two consecutive restarts
(every thread parked in futex_do_wait, HTTP accepting connections but never
answering) while the process that had been up for four days was unaffected.

These run the reentrant path on a worker thread with a join timeout, so the
regression fails the suite instead of hanging it.
"""

from __future__ import annotations

import gc
import threading

from zigbee_ninja.ingest.engine import LoopActivityLog

JOIN_TIMEOUT = 10.0


def _run_with_timeout(fn) -> bool:
    """True if fn finished; False if it is still stuck at the timeout."""
    done = threading.Event()

    def target():
        fn()
        done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(JOIN_TIMEOUT)
    return done.is_set()


def test_gc_callback_reentering_note_does_not_deadlock():
    # Exactly the production path: a collection lands while note() holds the
    # lock, and its stop callback records the pause by calling note() again.
    log = LoopActivityLog()

    def reenter():
        with log._lock:
            log._on_gc("start", {"generation": 0})
            log._on_gc("stop", {"generation": 0})

    assert _run_with_timeout(reenter), (
        "note() deadlocked when a gc callback re-entered it; the lock must be "
        "reentrant (threading.RLock)"
    )


def test_note_is_reentrant_from_within_a_real_collection():
    # The same thing without hand-driving the callbacks: install the real gc
    # hook and force a collection while the lock is held.
    log = LoopActivityLog()
    log.install_gc()

    def collect_under_lock():
        with log._lock:
            gc.collect()

    try:
        assert _run_with_timeout(collect_under_lock), (
            "a real collection while holding the lock deadlocked the thread"
        )
    finally:
        log.remove_gc()

    # The hook must also leave nothing behind for the next installer.
    assert log._on_gc not in gc.callbacks


def test_span_still_records_after_reentrant_use():
    log = LoopActivityLog()
    log.install_gc()
    try:
        with log.span("mqtt_message"):
            gc.collect()
    finally:
        log.remove_gc()
    assert "mqtt_message" in log.stats()["totals"]
