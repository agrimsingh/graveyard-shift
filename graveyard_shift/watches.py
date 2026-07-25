"""Cheap, ACU-free unblock conditions for blocked_upstream pins."""

from urllib.parse import quote

import httpx


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


def _check_none(watch: dict) -> tuple[bool, str]:
    return False, watch.get("note") or "no machine-checkable condition"


CHECKERS = {
    "npm_version": _check_npm_version,
    "none": _check_none,
}


def is_unblocked(watch: dict) -> tuple[bool, str]:
    checker = CHECKERS.get(watch.get("kind"), _check_none)
    return checker(watch)
