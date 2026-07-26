"""Dashboard and control surface. One page answers the leadership question:
is this working, what did it cost, where are the humans needed."""

import json
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import controller, store

app = FastAPI(title="graveyard-shift")


@app.get("/api/metrics")
def metrics() -> dict:
    with store.db() as conn:
        return store.metrics(conn)


@app.get("/api/runs")
def runs() -> list:
    with store.db() as conn:
        rows = conn.execute(
            "SELECT r.*, p.dependency, p.issue_number FROM runs r"
            " JOIN pins p ON p.id = r.pin_id ORDER BY r.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/events")
def events(limit: int = 100) -> list:
    with store.db() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


@app.post("/api/tick")
def trigger_tick() -> dict:
    """Manual trigger for demos and the replay workflow."""
    controller.tick()
    return {"ok": True, "at": time.time()}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    with store.db() as conn:
        summary = store.metrics(conn)
        run_rows = conn.execute(
            "SELECT r.*, p.dependency, p.issue_number FROM runs r"
            " JOIN pins p ON p.id = r.pin_id ORDER BY r.id DESC"
        ).fetchall()
        event_rows = conn.execute(
            "SELECT e.*, p.dependency FROM events e"
            " LEFT JOIN runs r ON r.id = e.run_id"
            " LEFT JOIN pins p ON p.id = r.pin_id ORDER BY e.id DESC LIMIT 40"
        ).fetchall()

    state_colors = {
        "classifying": "#6366f1", "remediating": "#f59e0b", "awaiting_ci": "#0ea5e9",
        "green": "#10b981", "blocked_upstream": "#ef4444", "escalated": "#b91c1c",
    }
    def card(key: str, value) -> str:
        label = key.removesuffix("_s").replace("_", " ")
        if value is None:
            shown = "–"
        elif key.endswith("_s"):
            shown = f"{int(value) // 60}m {int(value) % 60}s"
        else:
            shown = value
        return (f"<div class='card'><div class='num'>{shown}</div>"
                f"<div class='label'>{label}</div></div>")

    cards = "".join(
        card(key, value) for key, value in summary.items() if key != "runs_by_state"
    )
    def row_html(r) -> str:
        confidence = f"{r['confidence']:.0%}" if r["confidence"] is not None else "–"
        pr = f"<a href='{r['pr_url']}'>PR</a>" if r["pr_url"] else "–"
        color = state_colors.get(r["state"], "#666")
        elapsed = int(r["updated_at"] - r["created_at"])
        return (
            f"<tr><td>{r['dependency']}</td>"
            f"<td><span class='pill' style='background:{color}'>{r['state']}</span></td>"
            f"<td>{r['classification'] or '–'}</td><td>{confidence}</td>"
            f"<td>{elapsed // 60}m {elapsed % 60}s</td><td>{r['attempts']}</td>"
            f"<td><a href='{r['session_url']}'>session</a></td><td>{pr}</td></tr>"
        )

    rows = "".join(row_html(r) for r in run_rows)
    feed = "".join(
        f"<li><code>{time.strftime('%m-%d %H:%M', time.localtime(e['at']))}</code> "
        f"<b>{e['kind']}</b> {e['dependency'] or ''} {e['detail'][:120]}</li>"
        for e in event_rows
    )
    return f"""<!doctype html><html><head><title>graveyard-shift</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font: 14px/1.5 -apple-system, sans-serif; margin: 2rem; background: #0b0f14; color: #e6edf3; }}
h1 {{ font-size: 1.3rem; }} a {{ color: #58a6ff; }}
.cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 1rem 0 2rem; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 18px; min-width: 110px; }}
.num {{ font-size: 1.6rem; font-weight: 700; }} .label {{ color: #8b949e; font-size: .75rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
td, th {{ padding: 8px 10px; border-bottom: 1px solid #21262d; text-align: left; }}
.pill {{ padding: 2px 8px; border-radius: 10px; color: #fff; font-size: .75rem; }}
ul {{ list-style: none; padding: 0; }} li {{ padding: 2px 0; color: #8b949e; }}
li b {{ color: #e6edf3; }}
</style></head><body>
<h1>graveyard-shift · Dependabot ignore-list audit</h1>
<div class="cards">{cards}</div>
<table><tr><th>pin</th><th>state</th><th>classification</th><th>confidence</th>
<th>elapsed</th><th>ci retries</th><th>devin</th><th>pr</th></tr>{rows}</table>
<h1>event feed</h1><ul>{feed}</ul>
</body></html>"""
