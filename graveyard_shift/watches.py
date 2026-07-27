"""Cheap, ACU-free unblock conditions for blocked_upstream pins."""

from urllib.parse import quote

import httpx

from . import gh


def _gh_auth() -> dict:
    # Public read works unauthenticated, but the rate limit is 60/hour shared
    # across every watched pin, so use the token when there is one.
    try:
        return {"Authorization": f"Bearer {gh._token()}"}
    except Exception:
        return {}


def parse(raw: dict | None) -> dict:
    """Normalize Devin's unblock_watch. Degrades to kind=none; never raises."""
    if not isinstance(raw, dict):
        return {"kind": "none", "note": "missing or non-object watch"}
    kind = raw.get("kind")
    if kind == "npm_version":
        package, min_version = raw.get("package"), raw.get("min_version")
        if isinstance(package, str) and package and isinstance(min_version, str) and min_version:
            return {"kind": "npm_version", "package": package, "min_version": min_version}
        return {"kind": "none", "note": "npm_version watch missing package or min_version"}
    if kind == "github_pr_merged":
        repo, number = raw.get("repo"), raw.get("pr_number")
        if isinstance(repo, str) and "/" in repo and isinstance(number, int):
            return {"kind": "github_pr_merged", "repo": repo, "pr_number": number}
        return {"kind": "none", "note": "github_pr_merged watch missing repo or pr_number"}
    if kind == "none":
        note = raw.get("note")
        if isinstance(note, str) and note:
            return {"kind": "none", "note": note}
        return {"kind": "none", "note": "none watch missing note"}
    return {"kind": "none", "note": f"unrecognized watch kind: {kind!r}"}


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _check_npm_version(watch: dict) -> tuple[bool, str]:
    package, min_version = watch["package"], watch["min_version"]
    url = f"https://registry.npmjs.org/{quote(package, safe='')}"
    try:
        # Abbreviated metadata: full packuments run to megabytes for old packages.
        response = httpx.get(
            url,
            timeout=15,
            headers={"Accept": "application/vnd.npm.install-v1+json"},
        )
        response.raise_for_status()
        latest = response.json()["dist-tags"]["latest"]
    except Exception as exc:
        return False, f"npm registry check failed for {package}: {exc}"
    if _version_tuple(latest) >= _version_tuple(min_version):
        return True, f"{package}@{latest} >= {min_version}"
    return False, f"{package}@{latest} < {min_version}"


def _check_github_pr_merged(watch: dict) -> tuple[bool, str]:
    """Not every blocker is a package release. The commonest gate in a mature
    repo is a migration pull request that has not landed yet, so watch it
    directly rather than re-asking Devin on a timer."""
    repo, number = watch["repo"], watch["pr_number"]
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{repo}/pulls/{number}",
            timeout=15,
            headers={"Accept": "application/vnd.github+json", **_gh_auth()},
        )
        response.raise_for_status()
        pull = response.json()
    except Exception as exc:
        return False, f"github check failed for {repo}#{number}: {exc}"
    if pull.get("merged"):
        return True, f"{repo}#{number} merged at {pull.get('merged_at')}"
    return False, f"{repo}#{number} still {pull.get('state')}"


def _check_none(watch: dict) -> tuple[bool, str]:
    return False, watch.get("note") or "no machine-checkable condition"


CHECKERS = {
    "npm_version": _check_npm_version,
    "github_pr_merged": _check_github_pr_merged,
    "none": _check_none,
}


def is_unblocked(watch: dict) -> tuple[bool, str]:
    checker = CHECKERS.get(watch.get("kind"), _check_none)
    return checker(watch)
