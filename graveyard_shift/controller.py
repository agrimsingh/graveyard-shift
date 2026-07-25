"""The reconciler. Every tick is idempotent: it reads the world (Devin
sessions, GitHub checks, dependabot.yml) and advances each run's state
machine at most one step. Crashing mid-tick loses nothing."""

import json
import logging
import time

from . import config, dependabot, devin, gh, prompts, store

logger = logging.getLogger("graveyard")


def tick() -> None:
    with store.db() as conn:
        sync_pins(conn)
        reconcile_runs(conn)
        admit(conn)


def sync_pins(conn) -> None:
    yaml_text = gh.fetch_file(".github/dependabot.yml")
    for entry in dependabot.parse_ignore_entries(yaml_text):
        store.upsert_pin(conn, entry.dependency, entry.directory, entry.reason, entry.entry_hash)


def admit(conn) -> None:
    """Deterministic admission: capacity, allowlist, and the recheck clock."""
    capacity = config.MAX_CONCURRENT_RUNS - len(store.active_runs(conn))
    if capacity <= 0:
        return
    now = time.time()
    for pin in conn.execute("SELECT * FROM pins").fetchall():
        if capacity <= 0:
            return
        if config.PIN_ALLOWLIST and pin["dependency"] not in config.PIN_ALLOWLIST:
            continue
        if pin["recheck_after"] is not None and pin["recheck_after"] > now:
            continue
        last = store.run_for_pin(conn, pin["id"])
        if last is not None and (last["state"] in store.ACTIVE or pin["recheck_after"] is None):
            continue  # active, or terminal without an expired recheck clock
        launch(conn, pin)
        capacity -= 1


def launch(conn, pin) -> None:
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
        conn.execute("UPDATE pins SET issue_number = ? WHERE id = ?", (issue_number, pin["id"]))
    session = devin.create_session(
        prompt=prompts.classification_prompt(pin["dependency"], pin["reason"], issue_number),
        title=f"pin-audit: {pin['dependency']}",
        tags=["graveyard-shift", pin["dependency"]],
        structured_output_schema=prompts.CLASSIFICATION_SCHEMA,
    )
    store.create_run(conn, pin["id"], session["session_id"], session["url"])
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
    output = session.get("structured_output") or {}
    if not output.get("classification") or not devin.is_idle(session):
        return
    classification = output["classification"]
    confidence = output.get("confidence", 0)
    evidence = output.get("evidence", [])
    fields = {
        "classification": classification,
        "confidence": confidence,
        "evidence": json.dumps(evidence),
    }
    evidence_md = "\n".join(f"- {e.get('url', '')} {e['summary']}" for e in evidence)

    if classification in ("fixable_here", "stale_pin") and confidence >= config.CONFIDENCE_THRESHOLD:
        devin.send_message(run["session_id"], prompts.remediation_message(
            pin["dependency"], pin["issue_number"], output.get("proposed_validation", ""),
        ))
        store.transition(conn, run, store.REMEDIATING, classification, **fields)
        gh.set_labels(pin["issue_number"], ["pin-audit", classification.replace("_", "-")])
        gh.comment(pin["issue_number"],
                   f"**Devin classified this pin as `{classification}`** "
                   f"(confidence {confidence:.0%}). Remediation started.\n\n{evidence_md}\n\n"
                   f"Session: {run['session_url']}")
    elif classification == "blocked_upstream":
        recheck = time.time() + config.RECHECK_DAYS * 86400
        conn.execute("UPDATE pins SET recheck_after = ? WHERE id = ?", (recheck, pin["id"]))
        store.transition(conn, run, store.BLOCKED_UPSTREAM, **fields)
        gh.set_labels(pin["issue_number"], ["pin-audit", "blocked-upstream"])
        gh.comment(pin["issue_number"],
                   f"**Devin classified this pin as `blocked_upstream`** "
                   f"(confidence {confidence:.0%}). Will re-audit in "
                   f"{config.RECHECK_DAYS} days.\n\n{evidence_md}\n\n"
                   f"Session: {run['session_url']}")
    else:
        escalate(conn, run, pin, f"low confidence ({confidence:.0%}) on {classification}", fields)


def step_remediating(conn, run, pin, session) -> None:
    prs = session.get("pull_requests") or []
    if prs:
        pr_url = prs[0]["pr_url"]
        store.transition(conn, run, store.AWAITING_CI, pr_url, pr_url=pr_url)
        gh.comment(pin["issue_number"], f"Devin opened {pr_url}. Watching CI.")
    elif devin.is_idle(session) and session.get("status_detail") == "waiting_for_user":
        escalate(conn, run, pin, "session stuck waiting for user input during remediation")


def step_awaiting_ci(conn, run, pin, session) -> None:
    checks = gh.pr_checks(run["pr_url"])
    if checks["conclusion"] == "pending":
        return
    if checks["conclusion"] == "success":
        store.transition(conn, run, store.GREEN, run["pr_url"])
        gh.comment(pin["issue_number"],
                   f"CI is green on {run['pr_url']}. Ready for human review. "
                   f"ACUs consumed: {session['acus_consumed']}.")
    elif run["attempts"] < config.RETRY_LIMIT:
        devin.send_message(run["session_id"], prompts.ci_feedback_message(checks["failures"]))
        store.transition(conn, run, store.REMEDIATING, "ci feedback sent",
                         attempts=run["attempts"] + 1)
        gh.comment(pin["issue_number"],
                   f"CI failed (attempt {run['attempts'] + 1}). Failure logs sent back "
                   f"to the same Devin session.")
    else:
        escalate(conn, run, pin, "CI still failing after retry")


def escalate(conn, run, pin, reason: str, extra_fields: dict | None = None) -> None:
    store.transition(conn, run, store.ESCALATED, reason, **(extra_fields or {}))
    gh.set_labels(pin["issue_number"], ["pin-audit", "needs-human"])
    gh.comment(pin["issue_number"],
               f"**Escalating to a human**: {reason}.\n\nSession (full context): "
               f"{run['session_url']}")
