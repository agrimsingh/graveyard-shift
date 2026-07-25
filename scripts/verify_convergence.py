#!/usr/bin/env python3
"""Proves admission converges: a due pin starts exactly one Devin session no
matter how many ticks run. Regression cover for a duplicate-launch bug where an
expired due date was never consumed, so every tick spawned another session.

Runs against a throwaway database with the Devin and GitHub calls stubbed.
Usage: .venv/bin/python scripts/verify_convergence.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graveyard_shift import config  # noqa: E402

config.DB_PATH = Path(tempfile.mkdtemp()) / "converge.sqlite3"
config.PIN_ALLOWLIST = []
config.MAX_CONCURRENT_RUNS = 10

from graveyard_shift import controller, devin, gh, store, watches  # noqa: E402

launched: list[str] = []


def fake_create_session(prompt, title, tags, structured_output_schema, max_acu_limit=0):
    session_id = f"sess{len(launched)}"
    launched.append(title)
    return {"session_id": session_id, "url": f"https://app.devin.ai/sessions/{session_id}"}


devin.create_session = fake_create_session
gh.create_issue = lambda title, body, labels: 99

PAST = 1_000_000.0
FUTURE = 9_999_999_999.0
KEEP = object()


def seed(dependency: str, state: str | None, due_at, watch=None, entry_hash="h1") -> None:
    with store.db() as conn:
        pin = store.upsert_pin(conn, dependency, "/superset-frontend/", "reason", entry_hash)
        if state is not None:
            run_id = store.create_run(conn, pin["id"], "old-session", "https://old")
            conn.execute("UPDATE runs SET state = ? WHERE id = ?", (state, run_id))
        conn.execute("UPDATE pins SET watch = ?, issue_number = 99 WHERE id = ?",
                     (watch, pin["id"]))
        if due_at is not KEEP:
            conn.execute("UPDATE pins SET due_at = ? WHERE id = ?", (due_at, pin["id"]))


def ticks(count: int) -> int:
    before = len(launched)
    for _ in range(count):
        with store.db() as conn:
            controller.admit(conn)
    return len(launched) - before


def case(name: str, expected: int, actual: int) -> bool:
    ok = expected == actual
    print(f"{'PASS' if ok else 'FAIL'}  {name}: expected {expected} launches, got {actual}")
    return ok


results = []

seed("fresh-pin", None, KEEP)
results.append(case("newly discovered pin starts one session over 5 ticks", 1, ticks(5)))

seed("expired-due", store.GREEN, PAST)
results.append(case("expired due date is consumed, not replayed", 1, ticks(5)))

seed("settled-green", store.GREEN, None)
results.append(case("settled green pin never re-audits", 0, ticks(5)))

watches.is_unblocked = lambda watch: (
    watch["package"] == "shipped-pkg", f"stub verdict for {watch['package']}"
)


def npm_watch(package: str) -> str:
    return f'{{"kind": "npm_version", "package": "{package}", "min_version": "1.0.0"}}'


seed("watch-closed", store.BLOCKED_UPSTREAM, FUTURE, npm_watch("silent-pkg"))
results.append(case("unmet watch does not re-audit", 0, ticks(5)))

seed("watch-open", store.BLOCKED_UPSTREAM, FUTURE, npm_watch("shipped-pkg"))
results.append(case("met watch re-audits exactly once, ignoring the timer", 1, ticks(5)))

seed("changed-entry", store.GREEN, None)
ticks(1)
seed("changed-entry", None, KEEP, entry_hash="h2")
results.append(case("changed dependabot entry re-arms exactly once", 1, ticks(5)))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
