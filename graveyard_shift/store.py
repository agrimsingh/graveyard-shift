"""SQLite persistence. Pins are the audit surface, runs are the work,
events are the append-only trail the dashboard reads."""

import json
import sqlite3
import time
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
    attempts INTEGER NOT NULL DEFAULT 0,
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
"""


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


def create_run(conn, pin_id: int, session_id: str, session_url: str) -> int:
    now = time.time()
    # Launching consumes the due date. Without this a pin whose due date has
    # passed is re-admitted on every tick, one Devin session per pass.
    conn.execute("UPDATE pins SET due_at = NULL WHERE id = ?", (pin_id,))
    cursor = conn.execute(
        "INSERT INTO runs (pin_id, session_id, session_url, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (pin_id, session_id, session_url, CLASSIFYING, now, now),
    )
    log(conn, cursor.lastrowid, "run_created", session_url)
    return cursor.lastrowid


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
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET {sets}, updated_at = ? WHERE id = ?",
        (*fields.values(), time.time(), run_id),
    )


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


def run_for_pin(conn, pin_id: int):
    return conn.execute(
        "SELECT * FROM runs WHERE pin_id = ? ORDER BY id DESC LIMIT 1", (pin_id,)
    ).fetchone()


def metrics(conn) -> dict:
    by_state = dict(
        conn.execute("SELECT state, COUNT(*) FROM runs GROUP BY state").fetchall()
    )
    greens = conn.execute(
        "SELECT r.created_at, e.at, r.acus FROM runs r JOIN events e ON e.run_id = r.id"
        " AND e.kind = 'state:green' WHERE r.state = 'green'"
    ).fetchall()
    time_to_green = [e - c for c, e, _ in greens]
    classified = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE classification IS NOT NULL"
    ).fetchone()[0]
    actionable = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE classification IN ('fixable_here', 'stale_pin')"
    ).fetchone()[0]
    return {
        "pins_total": conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0],
        "runs_by_state": by_state,
        "classified": classified,
        "actionable": actionable,
        "actionable_rate": round(actionable / classified, 2) if classified else None,
        "green_prs": len(greens),
        "avg_time_to_green_s": round(sum(time_to_green) / len(time_to_green)) if time_to_green else None,
        "acus_per_green": round(sum(a for _, _, a in greens) / len(greens), 1) if greens else None,
        "total_acus": conn.execute("SELECT COALESCE(SUM(acus), 0) FROM runs").fetchone()[0],
        "escalations": by_state.get(ESCALATED, 0),
    }
