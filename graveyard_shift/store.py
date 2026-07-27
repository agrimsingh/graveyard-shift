"""SQLite persistence. Pins are the audit surface, runs are the work,
events are the append-only trail the dashboard reads."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

# Run lifecycle. Terminal states have no outgoing transitions.
CLASSIFYING = "classifying"
REMEDIATING = "remediating"
AWAITING_CI = "awaiting_ci"
GREEN = "green"
BLOCKED_UPSTREAM = "blocked_upstream"
ESCALATED = "escalated"

TERMINAL = {GREEN, BLOCKED_UPSTREAM, ESCALATED}
ACTIVE = {CLASSIFYING, REMEDIATING, AWAITING_CI}

TRANSITIONS = {
    CLASSIFYING: {REMEDIATING, BLOCKED_UPSTREAM, ESCALATED},
    REMEDIATING: {AWAITING_CI, ESCALATED},
    AWAITING_CI: {GREEN, REMEDIATING, ESCALATED},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY,
    dependency TEXT NOT NULL UNIQUE,
    directory TEXT NOT NULL,
    reason TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    due_at REAL,
    issue_number INTEGER,
    watch TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    pin_id INTEGER NOT NULL REFERENCES pins(id),
    session_id TEXT NOT NULL,
    session_url TEXT NOT NULL,
    state TEXT NOT NULL,
    classification TEXT,
    confidence REAL,
    evidence TEXT NOT NULL DEFAULT '[]',
    pr_url TEXT,
    judged_sha TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    stop_attempts INTEGER NOT NULL DEFAULT 0,
    acus REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admission_claims (
    pin_id INTEGER PRIMARY KEY REFERENCES pins(id),
    group_key TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    claimed_at REAL NOT NULL
);
"""


class AdmissionClaimLostError(RuntimeError):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(f"admission claim {token} is no longer active")


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pins)")}
    if "recheck_after" in cols and "due_at" not in cols:
        conn.execute("ALTER TABLE pins RENAME COLUMN recheck_after TO due_at")
    if "watch" not in cols:
        conn.execute("ALTER TABLE pins ADD COLUMN watch TEXT")
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "judged_sha" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN judged_sha TEXT")
    if "stop_attempts" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN stop_attempts INTEGER NOT NULL DEFAULT 0")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_pin(conn, dependency: str, directory: str, reason: str, entry_hash: str) -> dict:
    row = conn.execute("SELECT * FROM pins WHERE dependency = ?", (dependency,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO pins (dependency, directory, reason, entry_hash, due_at)"
            " VALUES (?, ?, ?, ?, 0)",
            (dependency, directory, reason, entry_hash),
        )
        log(conn, None, "pin_discovered", dependency)
    elif row["entry_hash"] != entry_hash:
        conn.execute(
            "UPDATE pins SET reason = ?, entry_hash = ?, due_at = 0, watch = NULL WHERE id = ?",
            (reason, entry_hash, row["id"]),
        )
        log(conn, None, "pin_changed", dependency)
    return conn.execute("SELECT * FROM pins WHERE dependency = ?", (dependency,)).fetchone()


def create_run(
    conn,
    pin_id: int,
    session_id: str,
    session_url: str,
    created_at: float | None = None,
) -> int:
    now = time.time()
    # The run row is only written once the issue and the Devin session exist, so
    # stamping it with `now` would start the clock after work already began and
    # quietly exclude that setup from every latency metric. Callers that admitted
    # the pin pass the moment they claimed it, which is when the trigger acted.
    created_at = now if created_at is None else created_at
    # Launching consumes the due date. Without this a pin whose due date has
    # passed is re-admitted on every tick, one Devin session per pass.
    conn.execute("UPDATE pins SET due_at = NULL WHERE id = ?", (pin_id,))
    cursor = conn.execute(
        "INSERT INTO runs (pin_id, session_id, session_url, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (pin_id, session_id, session_url, CLASSIFYING, created_at, now),
    )
    log(conn, cursor.lastrowid, "run_created", session_url)
    return cursor.lastrowid


def claim_pin(pin_id: int) -> str | None:
    token = uuid.uuid4().hex
    now = time.time()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Claims never expire automatically: after a crash, remote creation may
        # have succeeded even though no run was persisted. An operator must
        # resolve that uncertainty before deleting the claim and retrying.
        occupied = len(active_runs(conn)) + conn.execute(
            "SELECT COUNT(*) FROM admission_claims"
        ).fetchone()[0]
        if occupied >= config.MAX_CONCURRENT_RUNS:
            return None
        pin = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
        if pin is None:
            return None
        key = group_key(pin)
        if key in active_group_keys(conn):
            return None
        cursor = conn.execute(
            "INSERT OR IGNORE INTO admission_claims"
            " (pin_id, group_key, token, claimed_at) VALUES (?, ?, ?, ?)",
            (pin_id, key, token, now),
        )
        return token if cursor.rowcount == 1 else None


def save_claim_issue(token: str, issue_number: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE pins SET issue_number = ? WHERE id ="
            " (SELECT pin_id FROM admission_claims WHERE token = ?)",
            (issue_number, token),
        )


def finish_claim(token: str, session_id: str, session_url: str) -> int:
    with db() as conn:
        claim = conn.execute(
            "SELECT pin_id, claimed_at FROM admission_claims WHERE token = ?", (token,)
        ).fetchone()
        if claim is None:
            raise AdmissionClaimLostError(token)
        run_id = create_run(
            conn,
            claim["pin_id"],
            session_id,
            session_url,
            created_at=claim["claimed_at"],
        )
        conn.execute("DELETE FROM admission_claims WHERE token = ?", (token,))
        return run_id


def release_claim(token: str, detail: str) -> None:
    with db() as conn:
        claim = conn.execute(
            "SELECT pin_id FROM admission_claims WHERE token = ?", (token,)
        ).fetchone()
        if claim is None:
            return
        conn.execute("DELETE FROM admission_claims WHERE token = ?", (token,))
        log(conn, None, "launch_failed", detail)


def transition(conn, run: sqlite3.Row, new_state: str, detail: str = "", **fields) -> None:
    allowed = TRANSITIONS.get(run["state"], set())
    if new_state not in allowed:
        raise ValueError(f"illegal transition {run['state']} -> {new_state} (run {run['id']})")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET state = ?, updated_at = ?{', ' + sets if sets else ''} WHERE id = ?",
        (new_state, time.time(), *fields.values(), run["id"]),
    )
    log(conn, run["id"], f"state:{new_state}", detail)


def update_run(conn, run_id: int, **fields) -> None:
    """Heartbeat writes only. updated_at deliberately means "when the state
    last changed": the reconciler measures grace periods and timeouts against
    it, so touching it on every poll would hold those clocks at zero."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))


def log(conn, run_id, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events (run_id, at, kind, detail) VALUES (?, ?, ?, ?)",
        (run_id, time.time(), kind, detail),
    )


def active_runs(conn) -> list:
    placeholders = ",".join("?" * len(ACTIVE))
    return conn.execute(
        f"SELECT * FROM runs WHERE state IN ({placeholders})", tuple(ACTIVE)
    ).fetchall()


def group_key(pin) -> str:
    """Pins sharing a justification comment are one piece of work, because a
    Dependabot author writes one comment above the block of entries that move
    together. A blank comment groups nothing: that is the absence of a stated
    reason, not a shared one."""
    reason = (pin["reason"] or "").strip()
    return reason or f"ungrouped:{pin['id']}"


def active_group_keys(conn) -> set:
    """Group keys of pins that already have a run in flight."""
    placeholders = ",".join("?" * len(ACTIVE))
    return {
        group_key(row)
        for row in conn.execute(
            f"SELECT DISTINCT p.* FROM pins p JOIN runs r ON r.pin_id = p.id"
            f" WHERE r.state IN ({placeholders})",
            tuple(ACTIVE),
        )
    }


def run_for_pin(conn, pin_id: int):
    return conn.execute(
        "SELECT * FROM runs WHERE pin_id = ? ORDER BY id DESC LIMIT 1", (pin_id,)
    ).fetchone()


def _median(values: list[float]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle])
    return round((ordered[middle - 1] + ordered[middle]) / 2)


# A pin can be audited more than once, as an unblock watch fires or the
# question put to Devin improves. The dashboard reports where each pin stands
# now, so every metric reads the most recent run per pin. Superseded runs stay
# in the table and remain linkable from the run feed.
LATEST_RUNS = """SELECT * FROM runs r WHERE r.id = (
    SELECT id FROM runs WHERE pin_id = r.pin_id ORDER BY created_at DESC, id DESC LIMIT 1
)"""


def _elapsed_to(conn, kind: str, aggregate: str = "MIN") -> list[float]:
    """Seconds from a run's creation to when it reached a state.

    MIN answers "when did this first happen", which is what trigger-to-PR means.
    Settling on a verdict is a different question: a run that recorded green more
    than once had not actually finished the first time, so measuring from MIN
    would report the earliest claim rather than the one that held.
    """
    if aggregate not in ("MIN", "MAX"):
        raise ValueError(f"unsupported aggregate {aggregate!r}")
    rows = conn.execute(
        f"SELECT r.created_at, {aggregate}(e.at) FROM ({LATEST_RUNS}) r"
        " JOIN events e ON e.run_id = r.id WHERE e.kind = ? GROUP BY r.id",
        (kind,),
    ).fetchall()
    return [reached - created for created, reached in rows]


def metrics(conn) -> dict:
    def count(where: str) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM ({LATEST_RUNS}) WHERE {where}").fetchone()[0]

    by_state = dict(
        conn.execute(f"SELECT state, COUNT(*) FROM ({LATEST_RUNS}) GROUP BY state").fetchall()
    )
    audited = count("classification IS NOT NULL")
    actionable = count("classification IN ('fixable_here', 'stale_pin')")
    greens = by_state.get(GREEN, 0)
    first_pass = count(f"state = '{GREEN}' AND attempts = 0")
    claim_count, oldest_claimed_at = conn.execute(
        "SELECT COUNT(*), MIN(claimed_at) FROM admission_claims"
    ).fetchone()
    return {
        "pins_tracked": conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0],
        "audits_completed": audited,
        "actionable_rate": round(actionable / audited, 2) if audited else None,
        "green_prs": greens,
        # Throughput, which is the question a team actually asks of an
        # autonomous system: how long until this hands me something reviewable,
        # and how often does it get there without a second attempt.
        #
        # The name says "repair round" rather than "first-pass CI" because that
        # is all `attempts` counts: a CI failure fed back to Devin. It cannot see
        # a check run that passed while testing the wrong paths, so calling it a
        # first-pass rate would claim more than the data supports.
        "green_without_repair_round": f"{first_pass}/{greens}" if greens else None,
        "median_trigger_to_pr_s": _median(_elapsed_to(conn, f"state:{AWAITING_CI}")),
        "median_trigger_to_green_s": _median(
            _elapsed_to(conn, f"state:{GREEN}", aggregate="MAX")
        ),
        "admission_claims_in_flight": claim_count,
        "oldest_admission_claim_age_s": (
            round(max(0, time.time() - oldest_claimed_at))
            if oldest_claimed_at is not None else None
        ),
        "ci_retries": conn.execute(
            f"SELECT COALESCE(SUM(attempts), 0) FROM ({LATEST_RUNS})").fetchone()[0],
        "human_escalations": by_state.get(ESCALATED, 0),
        "runs_by_state": by_state,
    }
