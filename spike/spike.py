#!/usr/bin/env python3
"""Gating spike for graveyard-shift. Proves, in one Devin session:
  1. Devin can clone the superset fork.
  2. Structured output round-trips through the v3 API.
  3. A follow-up message resumes the session (CI-feedback loop viability).
  4. The plugin-chart-handlebars tests run in Devin's environment.

Rerunnable: session id is persisted in spike/state.json, so re-invoking
polls the existing session instead of paying for a new one.
Usage: python3 spike/spike.py [--reset]
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(__file__).resolve().parent / "state.json"
FORK = "agrimsingh/superset"
POLL_SECONDS = 30
TIMEOUT_SECONDS = 60 * 60

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "repo_accessible": {"type": "boolean"},
        "default_branch": {"type": "string"},
        "ignore_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dependency_name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["dependency_name", "reason"],
            },
        },
        "tests_ran": {"type": "boolean"},
        "tests_passed": {"type": "boolean"},
        "test_command": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["repo_accessible", "notes"],
}

PHASE1_PROMPT = f"""\
This is a read-only reconnaissance task. Do NOT modify code or open PRs.

1. Clone https://github.com/{FORK} (access is via this org's GitHub integration; \
if you cannot access it, report repo_accessible=false in structured output and stop).
2. Read .github/dependabot.yml and extract every entry under the npm \
superset-frontend ignore list: dependency name plus the comment explaining why \
it is pinned.
3. Provide structured output with repo_accessible, default_branch, \
ignore_entries, and notes. Leave the tests_* fields unset for now.
"""

PHASE2_MESSAGE = """\
Phase 2, still read-only: verify the test environment for the handlebars chart \
plugin. In superset-frontend, install dependencies and run the unit tests for \
plugins/plugin-chart-handlebars only (not the full suite). Update structured \
output: tests_ran, tests_passed, test_command (the exact command you used), and \
append timing/observations to notes.
"""


def env() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    missing = {"DEVIN_API_KEY", "DEVIN_ORG_ID"} - values.keys()
    if missing:
        sys.exit(f"missing in .env: {', '.join(sorted(missing))}")
    return values


def api(cfg: dict[str, str], method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"https://api.devin.ai/v3/organizations/{cfg['DEVIN_ORG_ID']}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {cfg['DEVIN_API_KEY']}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"{method} {path} -> {error.code}: {error.read().decode()}")


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def ensure_session(cfg: dict[str, str], state: dict) -> dict:
    if "session_id" in state:
        return state
    session = api(cfg, "POST", "/sessions", {
        "prompt": PHASE1_PROMPT,
        "title": "graveyard-shift spike: fork access + test env",
        "tags": ["graveyard-shift", "spike"],
        "max_acu_limit": 8,
        "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        "structured_output_required": True,
        "repos": [FORK],
    })
    state = {"session_id": session["session_id"], "phase2_sent": False}
    save_state(state)
    print(f"created session {session['session_id']}\nwatch live: {session['url']}")
    return state


def phase1_done(output: dict) -> bool:
    return bool(output.get("ignore_entries")) or output.get("repo_accessible") is False


def report(session: dict, output: dict) -> None:
    print("\n=== SPIKE RESULT ===")
    print(f"session: {session['url']}")
    print(f"acus_consumed: {session['acus_consumed']}")
    print(json.dumps(output, indent=2))
    gates = {
        "fork_access": output.get("repo_accessible") is True,
        "structured_output": bool(output),
        "message_resume": True,  # phase 2 output only exists if resume worked
        "tests_run": output.get("tests_ran") is True,
    }
    print("gates:", json.dumps(gates))
    sys.exit(0 if all(gates.values()) else 1)


def main() -> None:
    if "--reset" in sys.argv:
        STATE_FILE.unlink(missing_ok=True)
    cfg = env()
    state = ensure_session(cfg, load_state())
    deadline = time.time() + TIMEOUT_SECONDS
    last = ""
    while time.time() < deadline:
        session = api(cfg, "GET", f"/sessions/{state['session_id']}")
        output = session.get("structured_output") or {}
        status = f"{session['status']}/{session.get('status_detail')} acus={session['acus_consumed']}"
        if status != last:
            print(f"[{time.strftime('%H:%M:%S')}] {status}")
            last = status
        idle = session["status"] in ("suspended", "exit") or (
            session.get("status_detail") in ("waiting_for_user", "finished")
        )
        if output.get("repo_accessible") is False:
            report(session, output)
        if idle and phase1_done(output) and not state["phase2_sent"]:
            print("phase 1 complete; sending phase 2 message (tests resume check)")
            api(cfg, "POST", f"/sessions/{state['session_id']}/messages",
                {"message": PHASE2_MESSAGE})
            state["phase2_sent"] = True
            save_state(state)
        elif idle and state["phase2_sent"] and output.get("tests_ran") is not None:
            report(session, output)
        elif session["status"] == "error":
            report(session, output)
        time.sleep(POLL_SECONDS)
    sys.exit("spike timed out")


if __name__ == "__main__":
    main()
