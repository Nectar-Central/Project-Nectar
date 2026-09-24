"""No local may be READ before it is assigned.

`[duels] accept failed: cannot access local variable 'w2'` — the Accept log
line printed `w2` one statement before `w2 = await get_wager(...)`. Python
resolves `w2` as a function-local at compile time, so the failure only appears
at runtime, on one specific path, and py_compile is perfectly happy with it.

This module-level check walks each function body in source order and fails if
any local is loaded before its first store. It is deliberately narrow: nested
function/class bodies are their own scopes and are skipped, parameters and
imports count as already-assigned, and `global`/`nonlocal` names are ignored.
The whole module must be clean, so the next stray `w2` fails here instead of in
production.
"""
import ast
from pathlib import Path

import pytest

DUELS = Path("companion/bot/duels.py")


def _assigned_params_and_globals(fn: ast.FunctionDef):
    names = {a.arg for a in fn.args.posonlyargs}
    names |= {a.arg for a in fn.args.args}
    names |= {a.arg for a in fn.args.kwonlyargs}
    for extra in (fn.args.vararg, fn.args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            names |= set(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Nested scopes own their names; don't count them as stores here.
            if node is not fn:
                names.add(node.name)
    return names


def _order_stores_and_loads(body):
    """First store / first load line per name, in source order.

    Nested function, class, lambda and comprehension bodies are skipped (they
    are separate scopes in py3); everything else — branches, loops, try blocks —
    is real, because a store inside a branch that may not run is exactly the
    hazard. `except ... as e` counts as a store at the handler: py3 binds that
    name, the AST just doesn't model it as a Name node.
    """
    first_store, first_load = {}, {}

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda,
                                  ast.ListComp, ast.SetComp, ast.DictComp,
                                  ast.GeneratorExp)):
                continue  # own scope
            if isinstance(child, ast.ExceptHandler) and child.name:
                first_store.setdefault(str(child.name), child.lineno)
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, ast.Store):
                    first_store.setdefault(child.id, child.lineno)
                elif isinstance(child.ctx, ast.Load):
                    first_load.setdefault(child.id, child.lineno)
            visit(child)
    visit(body)
    return first_store, first_load


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _problems(src: str):
    tree = ast.parse(src)
    found = []
    for fn in _functions(tree):
        known = _assigned_params_and_globals(fn)
        stores, loads = _order_stores_and_loads(fn)
        for name, load_line in sorted(loads.items(), key=lambda p: p[1]):
            if name in known:
                continue
            store_line = stores.get(name)
            if store_line is None:
                continue
            if load_line < store_line:
                found.append(f"{fn.name}(): '{name}' read at {load_line}, "
                             f"assigned at {store_line}")
    return found


def test_no_local_is_read_before_it_is_assigned():
    problems = _problems(DUELS.read_text(encoding="utf-8"))
    assert not problems, ("read-before-assign in duels.py:\n  "
                          + "\n  ".join(problems))


def test_the_check_itself_catches_a_read_before_assign():
    # The guard is worthless if it cannot fail: prove it on the exact shape
    # that shipped (a log line reading a row fetched on the next statement).
    bad = """
async def accept(w):
    print(f"thread={w2.get('thread_id')}")
    w2 = await get_wager(w)
    return w2
"""
    found = _problems(bad)
    assert found and "'w2' read at 3, assigned at 4" in found[0]


def test_the_check_stays_quiet_on_legitimate_patterns():
    ok = """
async def f(w):
    try:
        w2 = await get(w)
    except Exception as e:
        print(e)
        w2 = w
    names = {n for n in ("a", "b") if n}
    for i in range(3):
        print(i)
    def inner():
        later = 1
        return later
    return w2, names, inner
"""
    assert _problems(ok) == []
