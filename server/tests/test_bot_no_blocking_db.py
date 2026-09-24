"""No blocking Supabase call may run on the bot's event loop.

`WARNING discord.gateway heartbeat blocked for more than 10 seconds` with a
traceback ending in `postgrest ... .execute()` is the signature: supabase-py is
a BLOCKING client, so a call made inline from a coroutine freezes the whole
loop — heartbeats stop, every interaction stalls, and discord.py logs the
gateway as dead. The proven offender was `alerts.alert_loop` polling
`bngc_alerts` with a bare `.execute()`; with httpx's 5s default plus
postgrest's retry that is 10s+ in one go.

Every DB touch on the event loop must go through `db._sb_call(lambda: ...)`,
which runs the blocking call in a worker thread (`_sb_call` is itself async, so
the lambda body is fine). Two ways code gets this wrong, both covered here:

1. A direct `.execute()` inside a coroutine. The proven offender was
   `alerts.alert_loop` polling `bngc_alerts`; with httpx's 5s default plus
   postgrest's retry that is 10s+ in one go, right over the heartbeat window.
2. A SYNC helper holding the blocking call, invoked from a coroutine. Just as
   fatal, and invisible to a scan that only looks at `async def`.

`database.py`'s aiosqlite calls (`await db.execute("SELECT ...")`) are genuinely
async and are excluded.
"""
import ast
from pathlib import Path

import pytest

BOT = Path("companion/bot")


def _async_sqlite_names(fn) -> set:
    """Names bound to an aiosqlite connection inside this function.

    `await db.execute("SELECT ...")` on an aiosqlite connection is genuinely
    async — not the blocking supabase-py client — and must not be flagged.
    """
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.AsyncWith, ast.With)):
            for item in node.items:
                if item.optional_vars is None:
                    continue
                src = ast.dump(item.context_expr)
                if "aiosqlite" in src:
                    tgt = item.optional_vars
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
    return names


def _root_name(call) -> str:
    node = call.func
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _direct_executes(fn, skip_lambdas: bool = True) -> list:
    """`.execute()` calls in this function that are neither in a lambda nor on
    an aiosqlite connection — i.e. the blocking supabase-py ones.

    `skip_lambdas=False` is for auditing a SYNC function, which by definition
    can't be passing its query through an offloading lambda."""
    hits = []
    sqlite = _async_sqlite_names(fn)

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if skip_lambdas and isinstance(child, ast.Lambda):
                continue  # runs in the worker thread via _sb_call
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                    and child.func.attr == "execute":
                if _root_name(child) not in sqlite:
                    hits.append(child.lineno)
            visit(child)
    visit(fn)
    return sorted(set(hits))


def _offenders(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for line in _direct_executes(fn):
            out.append((fn.name, line))
    return out


# Gateway handlers and background loops starve the heartbeat unattended (24/7);
# slash-command handlers stall the one command that invoked them. Both block the
# whole loop either way, so every module is held to the same standard.
BOT_MODULES = sorted(p.name for p in BOT.glob("*.py"))


@pytest.mark.parametrize("name", BOT_MODULES)
def test_no_blocking_call_in_any_coroutine(name):
    offenders = _offenders(BOT / name)
    assert not offenders, (
        f"{name} runs blocking supabase-py on the event loop "
        f"(use db._sb_call(lambda: ...)): {offenders}")


OFFLOOP = {"_sb_call", "to_thread", "run_in_executor"}


def _sync_blockers(path: Path):
    """Sync functions holding a blocking .execute() that are CALLED from a
    coroutine without an offload in between. These stall the loop exactly like
    a direct call does, and an async-def-only scan cannot see them."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sync_defs = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
    risks = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
        calls = {c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        called = {n.func.attr for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        if calls & OFFLOOP:
            continue  # this coroutine does offload — nothing to trace
        for name, sdef in sync_defs.items():
            if name not in calls | called:
                continue
            for ln in _direct_executes(sdef, False):
                risks.append((fn.name, name, ln))
    return sorted(set(risks))


@pytest.mark.parametrize("name", BOT_MODULES)
def test_no_sync_db_helper_is_called_from_the_loop(name):
    risks = _sync_blockers(BOT / name)
    assert not risks, (
        f"{name}: sync function holding a blocking .execute() is called from a "
        f"coroutine — make it async and use db._sb_call: {risks}")


# --- meta-tests: a guard that cannot fail is worse than no guard -------------

def _mod(tmp_path, src: str) -> Path:
    p = tmp_path / "synthetic_bot.py"
    p.write_text(src, encoding="utf-8")
    return p


def test_detector_flags_a_bare_call_in_a_coroutine(tmp_path):
    src = ("async def f(sb):\n"
           "    return sb.table('t').select('*').execute()\n")
    assert _offenders(_mod(tmp_path, src)) == [("f", 2)]


def test_detector_allows_the_offloaded_and_aiosqlite_forms(tmp_path):
    src = ("import aiosqlite\n"
           "async def offloaded(sb):\n"
           "    return await db._sb_call(lambda: sb.table('t').execute())\n"
           "async def sqlite_path():\n"
           "    async with aiosqlite.connect('x.db') as conn:\n"
           "        return await conn.execute('SELECT 1')\n")
    assert _offenders(_mod(tmp_path, src)) == []


def test_detector_flags_sync_helper_reached_from_a_coroutine(tmp_path):
    src = ("def helper(sb):\n"
           "    return sb.table('t').select('*').execute()\n"
           "async def command(sb):\n"
           "    return helper(sb)\n")
    assert _sync_blockers(_mod(tmp_path, src)) == [("command", "helper", 2)]


def test_detector_ignores_a_sync_helper_no_coroutine_calls(tmp_path):
    src = ("def helper(sb):\n"
           "    return sb.table('t').select('*').execute()\n"
           "async def command(sb):\n"
           "    return 1\n")
    assert _sync_blockers(_mod(tmp_path, src)) == []
