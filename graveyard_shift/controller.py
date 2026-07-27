"""The reconciler. Every tick is idempotent: it reads the world (Devin
sessions, GitHub checks, dependabot.yml) and advances each run's state
machine at most one step. Crashing mid-tick loses nothing."""

import json
import logging
import time

from . import config, dependabot, devin, gh, prompts, store, watches

logger = logging.getLogger("graveyard")


def tick() -> None:
    with store.db() as conn:
        try:
            sync_pins(conn)
        except Exception:
            # Discovering new pins is the least urgent thing a tick does. A
            # GitHub blip must not stop in-flight runs from being reconciled.
            logger.exception("pin sync failed; reconciling existing runs anyway")
        reconcile_runs(conn)
        admit(conn)


def sync_pins(conn) -> None:
    yaml_text = gh.fetch_file(".github/dependabot.yml")
    for entry in dependabot.parse_ignore_entries(yaml_text):
        store.upsert_pin(conn, entry.dependency, entry.directory, entry.reason, entry.entry_hash)


def admit(conn) -> None:
    """Deterministic admission. A pin runs when it is due, and launching clears
    the due date, so repeated ticks converge instead of relaunching forever."""
    capacity = config.MAX_CONCURRENT_RUNS - len(store.active_runs(conn))
    if capacity <= 0:
        return
    now = time.time()
    # A Dependabot author writes one comment above a block of entries because
    # those entries move together. Five React pins sharing one TODO are one
    # migration, not five, so admitting a second member while the first is in
    # flight buys a duplicate session and a conflicting pull request.
    busy_groups = store.active_group_keys(conn)
    for pin in conn.execute("SELECT * FROM pins").fetchall():
        if capacity <= 0:
            return
        if config.PIN_ALLOWLIST and pin["dependency"] not in config.PIN_ALLOWLIST:
            continue
        if store.group_key(pin) in busy_groups:
            continue
        last = store.run_for_pin(conn, pin["id"])
        if last is not None and last["state"] in store.ACTIVE:
            continue
        due_at = pin["due_at"]
        if last is None and due_at is None:
            # A null due date means "no scheduled reason to run". For a pin
            # that has never been audited at all there is a standing reason, so
            # it is due now. This also self-heals a pin whose row was written
            # before a launch failed partway through.
            due_at = 0
        if last is not None and last["state"] == store.BLOCKED_UPSTREAM:
            due_at = fire_watch(conn, pin, last) or due_at
        if due_at is None or due_at > now:
            continue
        launch(conn, pin)
        busy_groups.add(store.group_key(pin))
        capacity -= 1


def fire_watch(conn, pin, run) -> float | None:
    """Evaluate a stored unblock condition for zero ACUs. Firing clears the
    watch, so a permanently-true condition cannot re-audit on every tick."""
    if not pin["watch"]:
        return None
    flipped, reason = watches.is_unblocked(watches.parse(json.loads(pin["watch"])))
    if not flipped:
        return None
    now = time.time()
    conn.execute("UPDATE pins SET due_at = ?, watch = NULL WHERE id = ?", (now, pin["id"]))
    store.log(conn, run["id"], "watch_unblocked", reason)
    return now


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
            pin["dependency"], pin["issue_number"], output.get("proposed_validation", ""),
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
        pr_url = prs[-1]["pr_url"]
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
        return
    if checks["head_sha"] == run["judged_sha"]:
        # We already ruled on this commit and asked for a repair. GitHub will
        # keep reporting that same failure until Devin pushes, and acting on it
        # again would burn the retry budget on a verdict we have already used.
        return
    if checks["conclusion"] == "success":
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


def escalate(conn, run, pin, reason: str, extra_fields: dict | None = None) -> None:
    store.transition(conn, run, store.ESCALATED, reason, **(extra_fields or {}))
    gh.set_labels(pin["issue_number"], ["pin-audit", "needs-human"])
    gh.comment(pin["issue_number"],
               f"**Escalating to a human**: {reason}.\n\nSession (full context): "
               f"{run['session_url']}")
