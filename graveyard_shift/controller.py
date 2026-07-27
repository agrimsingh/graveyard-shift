"""Idempotent reconciliation of Devin sessions, GitHub checks, and Dependabot pins."""

import json
import logging
import threading
import time
from typing import TypedDict

from . import config, dependabot, devin, gh, prompts, store, watches

logger = logging.getLogger("graveyard")

_TICK_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_LABELS_READY = False


class ControllerStatus(TypedDict):
    ticks_completed: int
    last_tick_started_at: float | None
    last_tick_completed_at: float | None
    last_tick_error: str | None
    last_tick_error_at: float | None


_STATUS = ControllerStatus(ticks_completed=0, last_tick_started_at=None, last_tick_completed_at=None, last_tick_error=None, last_tick_error_at=None)


def status() -> ControllerStatus:
    with _STATUS_LOCK:
        return _STATUS.copy()


def tick() -> None:
    global _LABELS_READY
    with _TICK_LOCK:
        with _STATUS_LOCK:
            _STATUS["last_tick_started_at"] = time.time()
        tick_error = None
        try:
            if not _LABELS_READY:
                gh.ensure_labels()
                _LABELS_READY = True
            with store.db() as conn:
                try:
                    sync_pins(conn)
                except Exception as exc:
                    tick_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("pin sync failed; reconciling existing runs anyway")
                reconcile_runs(conn)
            admit()
        except Exception as exc:
            with _STATUS_LOCK:
                _STATUS["last_tick_error"] = f"{type(exc).__name__}: {exc}"
                _STATUS["last_tick_error_at"] = time.time()
            raise
        completed_at = time.time()
        with _STATUS_LOCK:
            _STATUS["ticks_completed"] += 1
            _STATUS["last_tick_completed_at"] = completed_at
            _STATUS["last_tick_error"] = tick_error
            _STATUS["last_tick_error_at"] = completed_at if tick_error else None


def sync_pins(conn) -> None:
    yaml_text = gh.fetch_file(".github/dependabot.yml")
    for entry in dependabot.parse_ignore_entries(yaml_text):
        store.upsert_pin(conn, entry.dependency, entry.directory, entry.reason, entry.entry_hash)


def admit(_conn=None) -> None:
    now = time.time()
    due_pins = []
    with store.db() as conn:
        for pin in conn.execute("SELECT * FROM pins").fetchall():
            if config.PIN_ALLOWLIST and pin["dependency"] not in config.PIN_ALLOWLIST:
                continue
            last = store.run_for_pin(conn, pin["id"])
            if last is not None and last["state"] in store.ACTIVE:
                continue
            due_at = pin["due_at"]
            if last is None and due_at is None:
                due_at = 0
            if last is not None and last["state"] == store.BLOCKED_UPSTREAM:
                due_at = fire_watch(conn, pin, last) or due_at
            if due_at is not None and due_at <= now:
                due_pins.append(pin)
    for pin in due_pins:
        claim_token = store.claim_pin(pin["id"])
        if claim_token is None:
            continue
        try:
            launch(pin, claim_token)
        except Exception as exc:
            store.release_claim(claim_token, f"{type(exc).__name__}: {exc}")
            raise


def fire_watch(conn, pin, run) -> float | None:
    """Evaluate a stored unblock condition once without spending Devin ACUs."""
    if not pin["watch"]:
        return None
    flipped, reason = watches.is_unblocked(watches.parse(json.loads(pin["watch"])))
    if not flipped:
        return None
    now = time.time()
    conn.execute("UPDATE pins SET due_at = ?, watch = NULL WHERE id = ?", (now, pin["id"]))
    store.log(conn, run["id"], "watch_unblocked", reason)
    return now


def launch(pin, claim_token: str) -> None:
    issue_number = pin["issue_number"]
    if issue_number is None:
        issue_number = gh.create_issue(
            title=f"[pin-audit] {pin['dependency']}: re-evaluate Dependabot ignore",
            body=(
                f"`{pin['dependency']}` is pinned in `.github/dependabot.yml` "
                f"({pin['directory']}).\n\n**Documented reason:**\n> {pin['reason']}\n\n"
                "This issue tracks an automated audit: Devin will verify whether the "
                "blocker still holds and remediate if it is actionable."
            ),
            labels=["pin-audit"],
        )
        store.save_claim_issue(claim_token, issue_number)
    session = devin.create_session(
        prompt=prompts.classification_prompt(pin["dependency"], pin["reason"], issue_number),
        title=f"pin-audit: {pin['dependency']}",
        tags=["graveyard-shift", pin["dependency"]],
        structured_output_schema=prompts.CLASSIFICATION_SCHEMA,
    )
    try:
        store.finish_claim(claim_token, session["session_id"], session["url"])
    except Exception:
        try:
            devin.stop_session(session["session_id"])
        except Exception:
            logger.exception("failed to stop untracked Devin session %s", session["session_id"])
        raise
    logger.info("launched %s for %s", session["session_id"], pin["dependency"])


def reconcile_runs(conn) -> None:
    for run in store.active_runs(conn):
        try:
            step(conn, run)
        except Exception:
            logger.exception("reconcile failed for run %s", run["id"])


def step(conn, run) -> None:
    session = devin.get_session(run["session_id"])
    store.update_run(conn, run["id"], acus=session["acus_consumed"])
    pin = conn.execute("SELECT * FROM pins WHERE id = ?", (run["pin_id"],)).fetchone()

    if devin.is_dead(session):
        escalate(conn, run, pin, f"session died: {session.get('status_detail')}")
        return

    if run["state"] == store.CLASSIFYING:
        step_classifying(conn, run, pin, session)
    elif run["state"] == store.REMEDIATING:
        step_remediating(conn, run, pin, session)
    elif run["state"] == store.AWAITING_CI:
        step_awaiting_ci(conn, run, pin, session)


def step_classifying(conn, run, pin, session) -> None:
    # Devin does not reliably pause after deciding; it often continues straight
    # into remediation. The verdict is the structured output, not an idle
    # session, so act on the artifact and let remediation catch up.
    output = session.get("structured_output") or {}
    if not output.get("classification"):
        if session["status"] == "exit":
            escalate(conn, run, pin, "session ended without returning a classification")
        return
    classification = output["classification"]
    confidence = output.get("confidence", 0)
    evidence = output.get("evidence", [])
    fields = {
        "classification": classification,
        "confidence": confidence,
        "evidence": json.dumps(evidence),
    }
    evidence_md = "\n".join(
        f"- {e.get('url', '')} {e.get('summary', '')}" for e in evidence
    )

    if classification in ("fixable_here", "stale_pin") and confidence >= config.CONFIDENCE_THRESHOLD:
        devin.send_message(run["session_id"], prompts.remediation_message(
            pin["dependency"], pin["issue_number"],
        ))
        store.transition(conn, run, store.REMEDIATING, classification, **fields)
        gh.set_labels(pin["issue_number"], ["pin-audit", classification.replace("_", "-")])
        gh.comment(pin["issue_number"],
                   f"**Devin classified this pin as `{classification}`** "
                   f"(confidence {confidence:.0%}). Remediation started.\n\n{evidence_md}\n\n"
                   f"Session: {run['session_url']}")
    elif classification == "blocked_upstream":
        watch = watches.parse(output.get("unblock_watch"))
        already_true, why = watches.is_unblocked(watch)
        if already_true:
            watch = {"kind": "none", "note": f"condition already met at classification ({why})"}
        if not stop_or_escalate(conn, run, pin):
            return
        conn.execute(
            "UPDATE pins SET due_at = ?, watch = ? WHERE id = ?",
            (time.time() + config.RECHECK_DAYS * 86400, json.dumps(watch), pin["id"]),
        )
        store.transition(conn, run, store.BLOCKED_UPSTREAM, **fields)
        gh.set_labels(pin["issue_number"], ["pin-audit", "blocked-upstream"])
        gh.comment(pin["issue_number"],
                   f"**Devin classified this pin as `blocked_upstream`** "
                   f"(confidence {confidence:.0%}). Will re-audit in "
                   f"{config.RECHECK_DAYS} days, or sooner if this watch "
                   f"clears:\n```json\n{json.dumps(watch, indent=2)}\n```\n\n"
                   f"{evidence_md}\n\n"
                   f"Session: {run['session_url']}")
    else:
        escalate(conn, run, pin, f"low confidence ({confidence:.0%}) on {classification}", fields)


def step_remediating(conn, run, pin, session) -> None:
    """Remediating means waiting for code we have not judged yet. On a repair
    round the pull request already exists, so its mere presence proves nothing;
    only a commit newer than the one we failed does."""
    prs = session.get("pull_requests") or []
    if prs:
        try:
            pr_url = prs[-1]["pr_url"]
            gh.pr_number(pr_url)
        except (KeyError, TypeError, ValueError):
            escalate(conn, run, pin, "Devin returned an invalid pull request")
            return
        if pr_url != run["pr_url"]:
            store.transition(conn, run, store.AWAITING_CI, pr_url, pr_url=pr_url)
            gh.comment(pin["issue_number"], f"Devin opened {pr_url}. Watching CI.")
            return
        if gh.pr_head_sha(pr_url) != run["judged_sha"]:
            store.transition(conn, run, store.AWAITING_CI, "new commit pushed")
            return

    waiting_for = time.time() - run["updated_at"]
    if session["status"] == "exit":
        escalate(conn, run, pin, "session ended without new code")
    elif waiting_for > config.REMEDIATION_TIMEOUT_SECONDS:
        # Devin does not always idle when stuck, so idleness alone cannot be
        # the only way out of this state.
        escalate(conn, run, pin, f"no new code after {int(waiting_for) // 60}m")
    elif devin.is_idle(session) and waiting_for > config.REMEDIATION_GRACE_SECONDS:
        # Devin idles briefly mid-task, so one idle observation is not stuck.
        escalate(conn, run, pin, "idle without new code past the grace period")


def step_awaiting_ci(conn, run, pin, session) -> None:
    checks = gh.pr_checks(run["pr_url"])
    if checks["conclusion"] == "pending":
        # A pull request touching no path any workflow watches never gets a
        # check run at all, and a stale_pin fix that only deletes an ignore
        # entry is exactly that shape. Waiting for a verdict that is never
        # coming would hold the slot forever.
        if not checks.get("has_checks") and (
            time.time() - run["updated_at"] > config.CI_APPEAR_TIMEOUT_SECONDS
        ):
            escalate(conn, run, pin,
                     "no CI ran for this change, so it cannot be verified automatically")
        return
    if checks["head_sha"] == run["judged_sha"]:
        # We already ruled on this commit and asked for a repair. GitHub will
        # keep reporting that same failure until Devin pushes, and acting on it
        # again would burn the retry budget on a verdict we have already used.
        return
    if checks["conclusion"] == "success":
        if not stop_or_escalate(conn, run, pin):
            return
        store.transition(conn, run, store.GREEN, run["pr_url"])
        elapsed = int(time.time() - run["created_at"])
        gh.comment(pin["issue_number"],
                   f"CI is green on {run['pr_url']}. Ready for human review. "
                   f"Trigger to green: {elapsed // 60}m {elapsed % 60}s, "
                   f"CI retries: {run['attempts']}.")
    elif run["attempts"] < config.RETRY_LIMIT:
        devin.send_message(run["session_id"], prompts.ci_feedback_message(checks["failures"]))
        store.transition(conn, run, store.REMEDIATING, "ci feedback sent",
                         attempts=run["attempts"] + 1, judged_sha=checks["head_sha"])
        gh.comment(pin["issue_number"],
                   f"CI failed (attempt {run['attempts'] + 1}). Failure logs sent back "
                   f"to the same Devin session.")
    else:
        escalate(conn, run, pin, "CI still failing after retry")


def stop_or_escalate(conn, run, pin) -> bool:
    """Stop the remote session before recording an unattended terminal state.

    An abandoned session keeps working: one of them opened a pull request long
    after this controller had stopped tracking it, so `green` and
    `blocked_upstream` may only be written once the session is verifiably
    stopped. But refusing to record anything until then strands the run in an
    ACTIVE state forever, holding a concurrency slot and re-attempting the same
    failing call every tick. So the attempt is bounded, and exhausting it is
    itself grounds for escalation, which frees the slot and tells a human that a
    session may still be live.

    Returns True when the caller may proceed to its terminal state.
    """
    try:
        devin.stop_session(run["session_id"])
        return True
    except Exception as exc:
        attempts = run["stop_attempts"] + 1
        store.update_run(conn, run["id"], stop_attempts=attempts)
        store.log(conn, run["id"], "stop_failed", f"attempt {attempts}: {exc}")
        if attempts < config.STOP_ATTEMPT_LIMIT:
            logger.warning("stop failed for run %s (attempt %s), retrying next tick",
                           run["id"], attempts)
            return False
        escalate(conn, run, pin,
                 f"could not confirm the Devin session stopped after {attempts} attempts",
                 extra_fields=None, stop_verified=False)
        return False


def escalate(conn, run, pin, reason: str, extra_fields: dict | None = None, *,
             stop_verified: bool | None = None) -> None:
    """Hand a run to a human. Always reaches ESCALATED.

    Escalation is the one terminal state that must never be blocked by a failing
    stop, because it is the path every other failure falls back to. When the
    session cannot be confirmed stopped, that becomes something the human is
    told rather than a reason to strand the run.
    """
    if stop_verified is None:
        try:
            devin.stop_session(run["session_id"])
            stop_verified = True
        except Exception as exc:
            store.log(conn, run["id"], "stop_failed", f"during escalation: {exc}")
            logger.warning("escalating run %s without a confirmed stop: %s", run["id"], exc)
            stop_verified = False
    warning = "" if stop_verified else (
        "\n\n:warning: This Devin session could not be confirmed stopped. It may "
        "still be working, and an abandoned session can still open a pull "
        "request. Check it and stop it manually."
    )
    store.transition(conn, run, store.ESCALATED, reason, **(extra_fields or {}))
    gh.set_labels(pin["issue_number"], ["pin-audit", "needs-human"])
    gh.comment(pin["issue_number"],
               f"**Escalating to a human**: {reason}.\n\nSession (full context): "
               f"{run['session_url']}{warning}")
