#!/usr/bin/env python3
"""Replays a full audit cycle against the real controller with the Devin and
GitHub calls faked, so the workflow can be inspected without API keys or ACUs.

Covers the three outcomes an operator cares about: a fixable pin that needs a
CI repair round before going green, a pin parked behind an upstream release
with a machine-checkable watch, and a pin escalated to a human on low
confidence.

Usage: .venv/bin/python scripts/simulate.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graveyard_shift import config  # noqa: E402

config.DB_PATH = Path(tempfile.mkdtemp()) / "simulate.sqlite3"
config.PIN_ALLOWLIST = []
config.MAX_CONCURRENT_RUNS = 10
config.REMEDIATION_GRACE_SECONDS = 0

from graveyard_shift import controller, devin, gh, store  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dependabot.yml"

WAITING = {"status": "running", "status_detail": "waiting_for_user"}
PR = "https://github.com/agrimsingh/superset/pull/42"

CLASSIFICATIONS = {
    "currencyformatter.js": {
        "classification": "fixable_here",
        "confidence": 0.9,
        "evidence": [{"url": "https://registry.npmjs.org/just-handlebars-helpers",
                      "summary": "peer range is stale; 2.x API is identical"}],
        "proposed_validation": "npm run test -- plugins/plugin-chart-handlebars",
        "unblock_watch": {"kind": "none", "note": "fixable in this repo"},
    },
    "react-checkbox-tree": {
        "classification": "blocked_upstream",
        "confidence": 0.85,
        "evidence": [{"url": "https://github.com/pmmmwh/react-refresh-webpack-plugin/pull/940",
                      "summary": "merged but unreleased; latest published is 0.6.2"}],
        "unblock_watch": {"kind": "npm_version",
                          "package": "@pmmmwh/react-refresh-webpack-plugin",
                          "min_version": "0.6.3"},
    },
    "mystery-pin": {
        "classification": "fixable_here",
        "confidence": 0.3,
        "evidence": [{"summary": "no documented reason; cannot determine intent"}],
        "unblock_watch": {"kind": "none", "note": "unknown"},
    },
}


class FakeDevin:
    """Sessions advance one scripted step per message, mirroring how Devin
    resumes: classify, then remediate, then repair CI."""

    def __init__(self):
        self.sessions = {}
        self.messages = []

    def create_session(self, prompt, title, tags, structured_output_schema, max_acu_limit=0):
        dependency = title.split(": ", 1)[1]
        session_id = f"sim-{dependency}"
        self.sessions[session_id] = {
            "session_id": session_id,
            "url": f"https://app.devin.ai/sessions/{session_id}",
            "acus_consumed": 0.0,
            "pull_requests": [],
            "structured_output": CLASSIFICATIONS[dependency],
            **WAITING,
        }
        return self.sessions[session_id]

    def get_session(self, session_id):
        return self.sessions[session_id]

    def send_message(self, session_id, message):
        self.messages.append((session_id, message))
        session = self.sessions[session_id]
        if not session["pull_requests"]:
            session["pull_requests"] = [{"pr_url": PR, "pr_state": "open"}]
            print(f"    devin resumed, opened {PR}")
        else:
            print("    devin resumed with CI failure logs, pushed a fix")
        return session


class FakeGitHub:
    """CI fails once on the remediation PR, then passes after Devin's repair."""

    def __init__(self):
        self.issues = {}
        self.check_calls = 0

    def fetch_file(self, path, ref=None):
        return FIXTURE.read_text()

    def ensure_labels(self):
        pass

    def create_issue(self, title, body, labels):
        number = len(self.issues) + 1
        self.issues[number] = {"title": title, "labels": labels, "comments": []}
        print(f"    opened issue #{number}: {title}")
        return number

    def comment(self, issue_number, body):
        self.issues[issue_number]["comments"].append(body)
        print(f"    commented on #{issue_number}: {body.splitlines()[0][:90]}")

    def set_labels(self, issue_number, labels):
        self.issues[issue_number]["labels"] = labels

    def pr_checks(self, pr_url):
        self.check_calls += 1
        if self.check_calls == 1:
            print("    CI failed on the PR")
            return {"conclusion": "failure", "failures": [{
                "name": "focused-tests",
                "url": "https://github.com/agrimsingh/superset/actions/runs/1",
                "summary": "FAIL formatCurrencyHelper.test.ts: expected $1,234,567.89",
            }]}
        print("    CI passed on the PR")
        return {"conclusion": "success", "failures": []}


fake_devin, fake_gh = FakeDevin(), FakeGitHub()
for name in ("create_session", "get_session", "send_message"):
    setattr(devin, name, getattr(fake_devin, name))
for name in ("fetch_file", "ensure_labels", "create_issue", "comment", "set_labels", "pr_checks"):
    setattr(gh, name, getattr(fake_gh, name))

print("Simulating the scheduled audit. Each tick reconciles every run by one step.\n")
for number in range(1, 7):
    print(f"tick {number}")
    controller.tick()
    with store.db() as conn:
        for run in conn.execute(
            "SELECT p.dependency, r.state FROM runs r JOIN pins p ON p.id = r.pin_id"
        ):
            print(f"    {run['dependency']:24s} {run['state']}")
    print()

with store.db() as conn:
    summary = store.metrics(conn)

print("Outcome")
for key, value in summary.items():
    if key != "runs_by_state":
        print(f"    {key:28s} {value}")

feedback = [m for _, m in fake_devin.messages if "CI failed" in m]
checks = {
    "one PR reached green": summary["green_prs"] == 1,
    "CI failure was fed back into the same session": len(feedback) == 1,
    "the repair took exactly one retry": summary["ci_retries"] == 1,
    "the upstream-blocked pin was parked, not escalated": summary["runs_by_state"].get(
        store.BLOCKED_UPSTREAM) == 1,
    "the low-confidence pin went to a human": summary["human_escalations"] == 1,
}
print()
for label, passed in checks.items():
    print(f"{'PASS' if passed else 'FAIL'}  {label}")
sys.exit(0 if all(checks.values()) else 1)
