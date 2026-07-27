#!/usr/bin/env python3
"""Trigger one reconciliation pass.

Reads GS_CONTROL_TOKEN from .env and sends it as a bearer token, so the token
never has to be exported into a shell or typed where a screen recording can see
it.

Usage: .venv/bin/python scripts/tick.py    (or: make tick)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

import service  # noqa: E402


def main() -> None:
    try:
        result = service.send_tick()
    except Exception as exc:
        print(f"tick failed: {exc}")
        sys.exit(1)
    print(f"tick accepted at {result['at']:.0f}")


if __name__ == "__main__":
    main()
