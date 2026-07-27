"""Dashboard and control surface. One page answers the leadership question:
is this working, what did it cost, where are the humans needed."""

import html
import secrets
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from . import config, controller, store

app = FastAPI(title="graveyard-shift")


@app.get("/api/health")
def health() -> dict:
    """Liveness of the reconciler, which a converged system cannot show through
    its data because it stops writing any."""
    controller_status = controller.status()
    return {
        **controller_status,
        "tick_seconds": config.TICK_SECONDS,
        "fork": config.FORK,
        "pin_allowlist": config.PIN_ALLOWLIST,
        "max_concurrent": config.MAX_CONCURRENT_RUNS,
    }


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
def trigger_tick(authorization: str | None = Header(default=None)) -> dict:
    """Manual trigger for demos and the replay workflow."""
    if not config.CONTROL_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="manual tick control is disabled until GS_CONTROL_TOKEN is configured",
        )
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(supplied, config.CONTROL_TOKEN)
    ):
        raise HTTPException(
            status_code=401,
            detail="valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    controller.tick()
    tick_status = controller.status()
    error_at = tick_status["last_tick_error_at"]
    started_at = tick_status["last_tick_started_at"]
    if (
        tick_status["last_tick_error"]
        and error_at is not None
        and started_at is not None
        and error_at >= started_at
    ):
        raise HTTPException(
            status_code=502,
            detail=f"manual tick completed with upstream error: "
            f"{tick_status['last_tick_error']}",
        )
    return {"ok": True, "at": time.time()}


def _text(value) -> str:
    return html.escape(str(value), quote=True)


def _safe_http_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return None
    return _text(value)


def _link(value, label: str) -> str:
    safe_url = _safe_http_url(value)
    return f"<a href='{safe_url}'>{_text(label)}</a>" if safe_url else "–"


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    with store.db() as conn:
        summary = store.metrics(conn)
        run_rows = conn.execute(
            "SELECT r.*, p.dependency, p.issue_number,"
            " r.id <> (SELECT id FROM runs WHERE pin_id = r.pin_id"
            "          ORDER BY created_at DESC, id DESC LIMIT 1) AS superseded"
            " FROM runs r JOIN pins p ON p.id = r.pin_id ORDER BY r.id DESC"
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
        return (f"<div class='card'><div class='num'>{_text(shown)}</div>"
                f"<div class='label'>{_text(label)}</div></div>")

    cards = "".join(
        card(key, value) for key, value in summary.items() if key != "runs_by_state"
    )
    def row_html(r) -> str:
        confidence = f"{r['confidence']:.0%}" if r["confidence"] is not None else "–"
        pr = _link(r["pr_url"], "PR")
        color = state_colors.get(r["state"], "#666")
        elapsed = int(r["updated_at"] - r["created_at"])
        # Re-audited pins keep their earlier runs visible, dimmed, so the
        # history stays readable without polluting the current picture.
        tr = "<tr style='opacity:.4' title='superseded by a later audit'>" if r["superseded"] else "<tr>"
        return (
            f"{tr}<td>{_text(r['dependency'])}</td>"
            f"<td><span class='pill' style='background:{color}'>{_text(r['state'])}</span></td>"
            f"<td>{_text(r['classification'] or '–')}</td><td>{_text(confidence)}</td>"
            f"<td>{elapsed // 60}m {elapsed % 60}s</td><td>{_text(r['attempts'])}</td>"
            f"<td>{_link(r['session_url'], 'session')}</td><td>{pr}</td></tr>"
        )

    rows = "".join(row_html(r) for r in run_rows)
    feed = "".join(
        f"<li><code>{_text(time.strftime('%m-%d %H:%M', time.localtime(e['at'])))}</code> "
        f"<b>{_text(e['kind'])}</b> {_text(e['dependency'] or '')} "
        f"{_text(e['detail'][:120])}</li>"
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
