"""Thin client for the Devin v3 organization API."""

import time

import httpx

from . import config

BASE = f"https://api.devin.ai/v3/organizations/{config.DEVIN_ORG_ID}"
HEADERS = {"Authorization": f"Bearer {config.DEVIN_API_KEY}"}


class SessionStillRunningError(RuntimeError):
    def __init__(self, session_id: str, status: str) -> None:
        self.session_id = session_id
        self.status = status
        super().__init__(f"Devin session {session_id} still has status {status!r}")


def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    response = httpx.request(
        method, f"{BASE}{path}", headers=HEADERS, json=json_body, timeout=60
    )
    response.raise_for_status()
    return response.json()


def create_session(
    prompt: str,
    title: str,
    tags: list[str],
    structured_output_schema: dict,
    max_acu_limit: int = config.MAX_ACU_PER_RUN,
) -> dict:
    return _request("POST", "/sessions", {
        "prompt": prompt,
        "title": title,
        "tags": tags,
        "repos": [config.FORK],
        "max_acu_limit": max_acu_limit,
        "structured_output_schema": structured_output_schema,
        "structured_output_required": True,
    })


def get_session(session_id: str) -> dict:
    return _request("GET", f"/sessions/{session_id}")


def terminate_session(session_id: str) -> dict:
    return _request("DELETE", f"/sessions/{session_id}")


def send_message(session_id: str, message: str) -> dict:
    # Suspended sessions auto-resume on message; this is the CI feedback channel.
    return _request("POST", f"/sessions/{session_id}/messages", {"message": message})


def is_idle(session: dict) -> bool:
    """True when the session has stopped working and won't progress without input."""
    return session["status"] in ("suspended", "exit") or session.get("status_detail") in (
        "waiting_for_user",
        "finished",
    )


def is_dead(session: dict) -> bool:
    return session["status"] == "error" or session.get("status_detail") in (
        "usage_limit_exceeded",
        "out_of_credits",
        "error",
    )


def is_stopped(session: dict) -> bool:
    return session["status"] == "exit"


def stop_session(session_id: str) -> None:
    session = get_session(session_id)
    if session.get("session_id") != session_id:
        raise SessionStillRunningError(session_id, "identity_mismatch")
    if is_stopped(session):
        return
    terminate_session(session_id)
    for delay in (0, 0.25, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        verified = get_session(session_id)
        if verified.get("session_id") != session_id:
            raise SessionStillRunningError(session_id, "identity_mismatch")
        if is_stopped(verified):
            return
    raise SessionStillRunningError(session_id, verified.get("status", "unknown"))
