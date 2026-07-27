#!/usr/bin/env python3
"""One command to get the machine ready to record the demo.

Checks that the audit has converged to the state the run sheet describes,
restarts the orchestrator scoped to the single pin used for the live trigger,
arms that pin, and confirms every tab the presenter will open actually loads.

Prints READY, or exactly one precise reason it is not.

Usage:
    .venv/bin/python scripts/demo_preflight.py
    .venv/bin/python scripts/demo_preflight.py --stop   # after recording
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

import httpx  # noqa: E402

from graveyard_shift import config, devin, store  # noqa: E402

import service  # noqa: E402

# The pin used for the on-camera trigger. It already has a green run, so the
# demo run supersedes it and can be discarded to make this script repeatable.
DEMO_PIN = "currencyformatter.js"

# The converged state the run sheet quotes on camera. If these drift, the
# narration is wrong, so this is an assertion rather than a display.
EXPECTED_AUDITS = 10
EXPECTED_BY_STATE = {store.GREEN: 5, store.BLOCKED_UPSTREAM: 3, store.ESCALATED: 2}

# Long enough that the scheduled reconciler will not fire during a recording.
QUIET_TICK_SECONDS = 86_400
TERMINATION_TIMEOUT_SECONDS = 15
TERMINATION_POLL_SECONDS = 0.5

TABS = [
    ("dashboard", service.DASHBOARD),
    # Anchored on the npm ignore block, so the whole graveyard is on screen
    # without scrolling for it on camera.
    ("dependabot.yml",
     f"https://github.com/{config.FORK}/blob/{config.DEFAULT_BRANCH}"
     "/.github/dependabot.yml#L12-L43"),
    ("PR #4 diff", f"https://github.com/{config.FORK}/pull/4/files"),
    ("PR #17, the React migration", f"https://github.com/{config.FORK}/pull/17"),
]


class NotReady(Exception):
    """A single precise reason the machine is not ready."""


def discard_rehearsal_run(conn) -> str | None:
    """Roll the demo pin back to its green baseline so this script can be run
    as many times as you like.

    Deletes only runs for DEMO_PIN that were created after its most recent
    green run. With no green run to fall back to there is no baseline to
    restore, so nothing is touched.
    """
    pin = conn.execute("SELECT * FROM pins WHERE dependency = ?", (DEMO_PIN,)).fetchone()
    if pin is None:
        return None

    runs = conn.execute(
        "SELECT * FROM runs WHERE pin_id = ? ORDER BY id", (pin["id"],)
    ).fetchall()
    baseline = [index for index, run in enumerate(runs) if run["state"] == store.GREEN]
    if not baseline:
        return None
    extras = runs[baseline[-1] + 1:]
    if not extras:
        return None

    terminated = []
    for run in extras:
        session_id = run["session_id"]
        try:
            before = devin.get_session(session_id)
        except httpx.HTTPError as exc:
            raise NotReady(
                f"could not inspect rehearsal session {session_id!r}; "
                "tracking was kept"
            ) from exc
        if before.get("session_id") != session_id:
            raise NotReady(
                f"rehearsal session identity mismatch for {session_id!r}; "
                "tracking was kept and no session was terminated"
            )
        try:
            if devin.is_stopped(before):
                continue
        except KeyError as exc:
            raise NotReady(
                f"rehearsal session {session_id!r} returned no status; "
                "tracking was kept"
            ) from exc
        try:
            devin.terminate_session(session_id)
        except httpx.HTTPError as exc:
            raise NotReady(
                f"could not terminate and verify rehearsal session {session_id!r}; "
                "tracking was kept"
            ) from exc

        deadline = time.monotonic() + TERMINATION_TIMEOUT_SECONDS
        while True:
            try:
                after = devin.get_session(session_id)
            except httpx.HTTPError as exc:
                raise NotReady(
                    f"could not verify terminated rehearsal session {session_id!r}; "
                    "tracking was kept"
                ) from exc
            if after.get("session_id") != session_id:
                raise NotReady(
                    f"rehearsal session identity mismatch after terminating "
                    f"{session_id!r}; tracking was kept"
                )
            try:
                if devin.is_stopped(after):
                    break
            except KeyError as exc:
                raise NotReady(
                    f"rehearsal session {session_id!r} returned no termination status; "
                    "tracking was kept"
                ) from exc
            if time.monotonic() >= deadline:
                raise NotReady(
                    f"rehearsal session {session_id!r} did not stop within "
                    f"{TERMINATION_TIMEOUT_SECONDS}s; tracking was kept"
                )
            time.sleep(TERMINATION_POLL_SECONDS)
        terminated.append(session_id)

    for run in extras:
        conn.execute("DELETE FROM events WHERE run_id = ?", (run["id"],))
        conn.execute("DELETE FROM runs WHERE id = ?", (run["id"],))
    detail = ", ".join(
        f"{run['session_url']}{' (terminated)' if run['session_id'] in terminated else ''}"
        for run in extras
    )
    return (f"rolled {DEMO_PIN} back to its green run, discarding "
            f"{len(extras)} rehearsal run(s): {detail}")


def check_no_claims(conn) -> None:
    """Refuse if any admission claim is outstanding.

    A claim outlives the process that made it and never expires, so a launch
    interrupted by the SIGTERM this script itself sends leaves a row that
    silently consumes a concurrency slot. At the demo's limit of one that makes
    every tick a no-op, which looks exactly like a dead trigger. Checking once up
    front is not enough for the same reason: the window this guards against is
    opened by our own restart, so it has to be re-checked after it.
    """
    claims = conn.execute(
        "SELECT c.token, c.claimed_at, p.dependency FROM admission_claims c"
        " JOIN pins p ON p.id = c.pin_id ORDER BY c.claimed_at"
    ).fetchall()
    if not claims:
        return
    held = "\n".join(
        f"      {row['dependency']}, claimed {int(time.time() - row['claimed_at'])}s ago"
        f"\n        sqlite3 {config.DB_PATH} \"DELETE FROM admission_claims"
        f" WHERE token = '{row['token']}'\""
        for row in claims
    )
    raise NotReady(
        f"{len(claims)} unresolved admission claim(s) are holding concurrency slots. "
        "Each means a launch was interrupted after claiming a pin, so a Devin "
        "session may exist with no run tracking it. For each one, find the "
        "untracked session for that pin at https://app.devin.ai/sessions and stop "
        "it, then run the matching delete. Delete only the claim you resolved: "
        "clearing the table would release claims whose sessions are still "
        f"unaccounted for, and those pins would be launched a second time.\n\n{held}"
    )


def check_converged(conn) -> None:
    check_no_claims(conn)

    active = store.active_runs(conn)
    if active:
        names = ", ".join(
            f"{row['dependency']} ({row['state']})"
            for row in conn.execute(
                "SELECT p.dependency, r.state FROM runs r JOIN pins p ON p.id = r.pin_id"
                f" WHERE r.id IN ({','.join(str(r['id']) for r in active)})"
            )
        )
        raise NotReady(
            f"{len(active)} run(s) still working: {names}. Wait for them to finish; "
            "recording now would show numbers that change mid-take."
        )

    summary = store.metrics(conn)
    if summary["audits_completed"] != EXPECTED_AUDITS:
        raise NotReady(
            f"{summary['audits_completed']} audits completed, run sheet says "
            f"{EXPECTED_AUDITS}. Either finish the audit or update the narration."
        )
    for state, expected in EXPECTED_BY_STATE.items():
        actual = summary["runs_by_state"].get(state, 0)
        if actual != expected:
            raise NotReady(
                f"{actual} runs in {state}, run sheet says {expected}. "
                "The narration would be wrong; check the dashboard."
            )


def set_demo_pin_due(conn, due_at: int | None) -> None:
    """Arm the pin with 0, or disarm it with None.

    Armed state lives in the database, so it outlives the process. A pin left
    armed by an earlier preflight would be claimed by the next startup pass
    before the presenter touched anything, which is why this runs both ways.
    """
    conn.execute(
        "UPDATE pins SET due_at = ?, watch = NULL WHERE dependency = ?", (due_at, DEMO_PIN)
    )


def check_tabs() -> None:
    for label, url in TABS:
        try:
            response = httpx.get(url, timeout=15, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise NotReady(f"{label} did not load: {exc}") from exc
        if response.status_code != 200:
            raise NotReady(f"{label} returned HTTP {response.status_code}: {url}")


def preflight() -> None:
    if not config.CONTROL_TOKEN:
        raise NotReady(
            "GS_CONTROL_TOKEN is unset; set it before preparing the authenticated "
            "on-camera tick"
        )

    notes = []

    with store.db() as conn:
        discarded = discard_rehearsal_run(conn)
        if discarded:
            notes.append(discarded)
        check_converged(conn)
        summary = store.metrics(conn)
        # Disarm before the restart so the startup pass finds nothing to do.
        set_demo_pin_due(conn, None)

    notes.append(service.stop())
    # Between the check above and that SIGTERM, the old process was still
    # reconciling and could have claimed a pin. Catch it now, while the failure
    # is still attributable to this restart.
    with store.db() as conn:
        check_no_claims(conn)
    pid = service.start({
        # Scoped so the tick triggered on camera starts exactly one session.
        "GS_PIN_ALLOWLIST": DEMO_PIN,
        "GS_MAX_CONCURRENT": "1",
        # The presenter's tick has to be the thing that starts the session. On
        # the normal 60s schedule the reconciler would quietly claim the armed
        # pin first and the live trigger would do nothing visible.
        "GS_TICK_SECONDS": str(QUIET_TICK_SECONDS),
    })
    notes.append(f"orchestrator running as pid {pid}, scoped to {DEMO_PIN}, "
                 "reconciler quiet so start audit is the trigger")

    # Arm only after the startup pass has finished, or it claims the pin first
    # and the live trigger has nothing to do.
    service.wait_for_first_tick()

    # Exercise the real on-camera command now, while the pin is still disarmed
    # so a pass costs nothing. Discovering a rejected token mid-recording is the
    # expensive way to learn the token only lives in .env.
    try:
        service.send_tick()
    except Exception as exc:
        raise NotReady(f"the authenticated tick used on camera does not work: {exc}") from exc
    notes.append("authenticated tick verified against the running service")

    with store.db() as conn:
        set_demo_pin_due(conn, 0)

    # Prove the handover state is what it claims: armed, and nothing started.
    # The claim check runs again here because the SIGTERM above is exactly what
    # can strand a claim, and a claim stranded after the first check would
    # otherwise reach READY as a silently dead trigger.
    with store.db() as conn:
        check_no_claims(conn)
        if store.active_runs(conn):
            raise NotReady(
                "a run started during preflight, so the live trigger would have "
                "nothing to do. Rerun this script."
            )
    notes.append(f"{DEMO_PIN} armed, nothing running, waiting for start audit")

    check_tabs()
    notes.append(f"all {len(TABS)} tabs load")

    print("READY\n")
    for note in notes:
        print(f"  - {note}")
    print(f"""
  On camera, in this order:
    1. {service.DASHBOARD}
       {summary['audits_completed']} audits, {summary['green_prs']} green PRs, """
          f"""{summary['runs_by_state'].get(store.BLOCKED_UPSTREAM, 0)} upstream blocks, """
          f"""{summary['runs_by_state'].get(store.ESCALATED, 0)} escalations
    2. click start audit on the dashboard
       (the one-time browser control fires the same reconciliation pass)
    3. after the page reloads, open the new Devin session and leave it working

  Afterwards: make demo-stop""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true",
                        help="stop the orchestrator and discard the demo run")
    args = parser.parse_args()

    try:
        if args.stop:
            print(service.stop())
            with store.db() as conn:
                discarded = discard_rehearsal_run(conn)
                set_demo_pin_due(conn, None)
            print(discarded or "no rehearsal run to discard")
            return
        preflight()
    except NotReady as exc:
        print(f"NOT READY\n\n  {exc}")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"NOT READY\n\n  {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
