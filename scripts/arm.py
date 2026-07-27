#!/usr/bin/env python3
"""Marks a pin due, so the next tick re-audits it.

Normally a pin becomes due on its own: a watch condition clears, its
dependabot.yml entry changes, or the recheck interval elapses. This forces the
same path by hand, which is useful for a demo or for re-running an audit after
the question put to Devin has improved.

Usage: .venv/bin/python scripts/arm.py react-checkbox-tree
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graveyard_shift import store  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(__doc__)

dependency = sys.argv[1]
with store.db() as conn:
    pin = conn.execute(
        "SELECT * FROM pins WHERE dependency = ?", (dependency,)
    ).fetchone()
    if pin is None:
        known = [r["dependency"] for r in conn.execute("SELECT dependency FROM pins")]
        sys.exit(f"no pin named {dependency!r}. Known pins:\n  " + "\n  ".join(known))
    active = [r for r in store.active_runs(conn) if r["pin_id"] == pin["id"]]
    if active:
        sys.exit(f"{dependency} already has a run in progress ({active[0]['state']})")
    conn.execute("UPDATE pins SET due_at = 0, watch = NULL WHERE id = ?", (pin["id"],))

print(f"{dependency} is due. The next tick will open a fresh Devin session for it.")
print("Force one now with: curl -X POST localhost:8090/api/tick")
