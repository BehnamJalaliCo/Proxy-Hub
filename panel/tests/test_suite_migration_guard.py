"""Every migration round trip in the tests puts the schema back to head, even when it fails.

``migrated_db`` is one schema for the whole session (tests/conftest.py). A test that fails between
a downgrade and its ``migrate head`` would leave every later database test on an old schema, so
one real failure would show as hundreds of ``relation ... does not exist``. Read from the source
(no database): each ``downgrade`` call must sit inside a ``try`` whose ``finally`` migrates to head.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _call_of(node: ast.AST, name: str) -> ast.Call | None:
    """``name(...)`` or ``asyncio.to_thread(name, ...)``; the call's own arguments come back."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == name:
        return node
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "to_thread"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == name
    ):
        return ast.Call(func=node.args[0], args=node.args[1:], keywords=node.keywords)
    return None


def _restores_head(statements: list[ast.stmt]) -> bool:
    for stmt in statements:
        for node in ast.walk(stmt):
            call = _call_of(node, "migrate")
            if call and call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "head":
                return True
    return False


def unguarded_downgrades(source: str) -> tuple[int, list[int]]:
    """(how many downgrade calls, the lines of those no ``finally: migrate head`` covers)."""
    seen: list[int] = []
    bad: list[int] = []

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Try | ast.TryStar):
            inner = guarded or _restores_head(node.finalbody)
            for part in (*node.body, *node.handlers, *node.orelse):
                visit(part, inner)
            for part in node.finalbody:
                visit(part, guarded)
            return
        if _call_of(node, "downgrade") is not None:
            seen.append(node.lineno)  # type: ignore[attr-defined]
            if not guarded:
                bad.append(node.lineno)  # type: ignore[attr-defined]
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(ast.parse(source), False)
    return len(seen), bad


def test_every_downgrade_in_the_tests_is_followed_by_migrate_head_in_a_finally() -> None:
    total, offenders = 0, []
    for path in sorted(TESTS.glob("test_*.py")):
        count, lines = unguarded_downgrades(path.read_text(encoding="utf-8"))
        total += count
        offenders += [f"{path.name}:{line}" for line in lines]
    assert total >= 10  # the scan does see the suite's round trips
    assert offenders == []


def test_negative_control_flags_a_round_trip_without_a_finally() -> None:
    bare = (
        "async def t():\n"
        "    await asyncio.to_thread(downgrade, '0010_domains')\n"
        "    assert tables == []\n"
        "    await asyncio.to_thread(migrate, 'head')\n"
    )
    disposes_only = (
        "async def t():\n"
        "    try:\n"
        "        await asyncio.to_thread(downgrade, '0010_domains')\n"
        "        await asyncio.to_thread(migrate, 'head')\n"
        "    finally:\n"
        "        await engine.dispose()\n"
    )
    guarded = (
        "async def t():\n"
        "    try:\n"
        "        await asyncio.to_thread(downgrade, '0010_domains')\n"
        "        assert tables == []\n"
        "    finally:\n"
        "        await asyncio.to_thread(migrate, 'head')\n"
    )
    assert unguarded_downgrades(bare) == (1, [2])
    assert unguarded_downgrades(disposes_only) == (1, [3])
    assert unguarded_downgrades(guarded) == (1, [])
