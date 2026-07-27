"""Thin GitHub REST client scoped to the fork."""

import base64
import subprocess
from urllib.parse import urlsplit

import httpx

from . import config

BASE = "https://api.github.com"

LABELS = {
    "pin-audit": "1d76db",
    "fixable-here": "0e8a16",
    "blocked-upstream": "d93f0b",
    "stale-pin": "fbca04",
    "needs-human": "b60205",
}


def _token() -> str:
    if config.GITHUB_TOKEN:
        return config.GITHUB_TOKEN
    return subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _request(method: str, path: str, json_body: dict | None = None) -> dict | list:
    response = httpx.request(
        method,
        f"{BASE}{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
        },
        json=json_body,
        timeout=30,
    )
    response.raise_for_status()
    return response.json() if response.text else {}


def fetch_file(path: str, ref: str = config.DEFAULT_BRANCH) -> str:
    data = _request("GET", f"/repos/{config.FORK}/contents/{path}?ref={ref}")
    return base64.b64decode(data["content"]).decode()


def ensure_labels() -> None:
    existing = {l["name"] for l in _request("GET", f"/repos/{config.FORK}/labels?per_page=100")}
    for name, color in LABELS.items():
        if name not in existing:
            _request("POST", f"/repos/{config.FORK}/labels", {"name": name, "color": color})


def create_issue(title: str, body: str, labels: list[str]) -> int:
    issue = _request(
        "POST", f"/repos/{config.FORK}/issues",
        {"title": title, "body": body, "labels": labels},
    )
    return issue["number"]


def comment(issue_number: int, body: str) -> None:
    _request("POST", f"/repos/{config.FORK}/issues/{issue_number}/comments", {"body": body})


def set_labels(issue_number: int, labels: list[str]) -> None:
    _request("PUT", f"/repos/{config.FORK}/issues/{issue_number}/labels", {"labels": labels})


def pr_number(pr_url: str) -> int:
    if not isinstance(pr_url, str):
        raise ValueError("pull request URL must be a string")
    try:
        parsed = urlsplit(pr_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid pull request URL") from exc
    parts = parsed.path.rstrip("/").split("/")
    expected_prefix = ["", *config.FORK.split("/"), "pull"]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 5
        or parts[:4] != expected_prefix
        or not parts[4].isdigit()
        or int(parts[4]) < 1
    ):
        raise ValueError(f"pull request URL is not in configured fork {config.FORK}")
    return int(parts[4])


def pr_head_sha(pr_url: str) -> str:
    number = pr_number(pr_url)
    return _request("GET", f"/repos/{config.FORK}/pulls/{number}")["head"]["sha"]


def pr_checks(pr_url: str) -> dict:
    """Aggregate check-run state for a PR's current head commit. Returns
    {conclusion, head_sha, failures: [...]}. conclusion is 'pending' |
    'success' | 'failure'. The head SHA matters: a verdict belongs to the
    commit it was computed from, not to the pull request."""
    number = pr_number(pr_url)
    pr = _request("GET", f"/repos/{config.FORK}/pulls/{number}")
    head_sha = pr["head"]["sha"]
    checks = _request(
        "GET", f"/repos/{config.FORK}/commits/{head_sha}/check-runs?per_page=100"
    )["check_runs"]
    if not checks or any(c["status"] != "completed" for c in checks):
        # has_checks separates "no result yet" from "no workflow watches these
        # paths, so no result is ever coming".
        return {
            "conclusion": "pending",
            "head_sha": head_sha,
            "has_checks": bool(checks),
            "failures": [],
        }
    failures = [
        {"name": c["name"], "url": c["html_url"], "summary": (c["output"]["summary"] or "")[:2000]}
        for c in checks
        if c["conclusion"] not in ("success", "neutral", "skipped")
    ]
    return {
        "conclusion": "failure" if failures else "success",
        "head_sha": head_sha,
        "has_checks": True,
        "failures": failures,
    }
