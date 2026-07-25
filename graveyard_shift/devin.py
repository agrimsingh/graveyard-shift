"""Thin client for the Devin v3 organization API."""

import httpx

from . import config

BASE = f"https://api.devin.ai/v3/organizations/{config.DEVIN_ORG_ID}"
HEADERS = {"Authorization": f"Bearer {config.DEVIN_API_KEY}"}


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
