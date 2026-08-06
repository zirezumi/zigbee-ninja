"""Structural guards on database writes.

The fix for the 2026-07-28/29 wedge is only as durable as the discipline it
rests on, and that discipline is invisible at a call site: `conn.execute(...)`
plus `conn.commit()` looks completely fine and reintroduces the bug. So it is
checked here rather than left to review, because the failure it causes is silent
(writes vanish, one connection at a time) and took 29 hours to notice once.
"""

import ast
from pathlib import Path

import zigbee_ninja

PACKAGE = Path(zigbee_ninja.__file__).parent

# Database.write/_migrate own the commits; everything else goes through write().
COMMIT_ALLOWED_IN = {"store/db.py"}


def _sources() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        out.append((str(path.relative_to(PACKAGE)), ast.parse(path.read_text())))
    return out


def _is_write_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
        and not node.args
        and not node.keywords
    )


def test_no_commit_outside_the_write_helper():
    offenders = []
    for name, tree in _sources():
        if name in COMMIT_ALLOWED_IN:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit"
            ):
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "commit() outside Database.write() at "
        + ", ".join(offenders)
        + ". A write that raises before its commit leaves the transaction open and "
        "wedges that thread's connection permanently; use `with db.write() as conn:`."
    )


def test_no_write_block_nests_inside_another():
    """write() rolls back an inherited transaction on entry, which is only safe
    while no outer write() block is holding uncommitted work."""
    offenders = []
    for name, tree in _sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            if not any(_is_write_call(item.context_expr) for item in node.items):
                continue
            own = {id(item.context_expr) for item in node.items}
            for inner in ast.walk(node):
                if id(inner) in own or inner is node:
                    continue
                if _is_write_call(inner):
                    offenders.append(
                        f"{name}:{inner.lineno} inside the block at line {node.lineno}"
                    )
    assert not offenders, "nested write() blocks at " + ", ".join(offenders)


# -- loop-thread write discipline ---------------------------------------------
#
# Checked at RUNTIME, not by AST, and the reason matters. Writers reach the
# database from the flush worker, API threadpool threads, the detector thread
# and constructor-injected callbacks. That last one is invisible to any call
# graph, and it is precisely where the defect was: probe heartbeats reached
# Database.write() through a callback the Engine handed to ProbeIngest, so no
# static rule following `Engine._handle_message` would ever have seen it.
# A thread-identity check has no blind spots and no false positives.


def test_ingest_path_issues_no_writes_on_the_loop_thread(client):
    """A write on the event loop waits on the WAL lock with a 5 s busy_timeout
    and stalls the loop behind it. Measured 5,011.9 ms on probe heartbeats,
    which made them the top loop-stall source: the timeout plus overhead.
    Nothing on the ingest path may write inline."""
    import json

    engine = client.app.state.engine
    db = client.app.state.db
    # start() normally does this; the test client's lifespan already ran it,
    # but pin it to THIS thread so the assertion is about the code under test
    # rather than about which thread pytest happens to use.
    db.mark_loop_thread()
    db.loop_thread_writes = 0

    info = {"version": "2.3.0", "network": {"channel": 15}, "config": {}}
    engine.on_message("z2m-test/bridge/info", json.dumps(info).encode())
    engine.on_message(
        "z2m-test/bridge/devices",
        json.dumps([{"ieee_address": "0x1", "friendly_name": "lamp", "type": "Router"}]).encode(),
    )
    # The regression case: a probe heartbeat. Before the fix this wrote inline.
    engine.on_message(
        "z2m-test/zigbee-ninja/probe/heartbeat",
        json.dumps({"version": "0.4", "hooks": [], "seq": 1}).encode(),
    )
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    engine.on_message("z2m-test/lamp", b'{"state":"ON","brightness":10}')

    assert db.loop_thread_writes == 0, (
        f"ingest wrote to sqlite on the event loop: {db.last_loop_thread_write}"
    )


def test_the_loop_thread_guard_actually_fires(client):
    """A guard nobody has seen fail is a guard nobody knows works. This pins
    that the check catches a real inline write rather than passing because
    loop_thread_id was never set."""
    db = client.app.state.db
    db.mark_loop_thread()
    db.loop_thread_writes = 0
    with db.write() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _guard_probe (x INTEGER)")
    assert db.loop_thread_writes == 1
    assert db.last_loop_thread_write and "test_write_discipline.py" in db.last_loop_thread_write
