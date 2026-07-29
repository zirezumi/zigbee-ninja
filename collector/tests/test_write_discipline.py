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
