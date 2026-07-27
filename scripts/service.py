"""Start and stop the orchestrator through a PID file.

The point of the PID file is identity. Killing whatever happens to hold port
8090 is fine until the day it is holding something else, so every signal here
goes to a process this module started and can still recognise.
"""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from graveyard_shift import config

RUN_DIR = config.ROOT / ".run"
PID_FILE = RUN_DIR / "orchestrator.pid"
LOG_FILE = RUN_DIR / "orchestrator.log"
DASHBOARD = f"http://localhost:{config.PORT}"

# Any process we are willing to signal has this on its command line.
FINGERPRINT = "graveyard_shift"


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    port: int
    database: str
    start_time: str


def _command_of(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return result.stdout.strip()


def _process_state(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "state="], capture_output=True, text=True
    )
    return result.stdout.strip()


def _start_time_of(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    return " ".join(result.stdout.split())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    if _process_state(pid).startswith("Z"):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        return False
    return True


def _pid_on_port() -> int | None:
    """Identify, never blind-kill. The caller decides what to do about it."""
    result = subprocess.run(
        ["lsof", "-t", f"-i:{config.PORT}", "-sTCP:LISTEN"],
        capture_output=True, text=True,
    )
    pids = [int(line) for line in result.stdout.split() if line.isdigit()]
    return pids[0] if pids else None


def running_pid() -> int | None:
    """The orchestrator we manage, if it is still up."""
    record = _recorded_process()
    if record is None:
        return None
    _require_current_identity(record)
    _require_same_process(record)
    if _alive(record.pid) and FINGERPRINT in _command_of(record.pid):
        return record.pid
    return None


def _recorded_process() -> ProcessRecord | None:
    if not PID_FILE.exists():
        return None
    raw = PID_FILE.read_text()
    if raw.strip().isdigit():
        raise RuntimeError(
            f"legacy PID file {PID_FILE} has no launch identity; "
            "refusing to signal it"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid orchestrator PID metadata in {PID_FILE}") from exc
    match data:
        case {
            "pid": int(pid),
            "port": int(port),
            "database": str(database),
            "start_time": str(start_time),
        }:
            return ProcessRecord(
                pid=pid,
                port=port,
                database=database,
                start_time=start_time,
            )
        case _:
            raise RuntimeError(f"invalid orchestrator PID metadata in {PID_FILE}")


def _require_current_identity(record: ProcessRecord) -> None:
    current_database = str(config.DB_PATH.resolve())
    if record.port != config.PORT or record.database != current_database:
        raise RuntimeError(
            "orchestrator launch identity mismatch: "
            f"recorded port={record.port}, database={record.database!r}; "
            f"current port={config.PORT}, database={current_database!r}. "
            "No process was signalled."
        )


def _require_same_process(record: ProcessRecord) -> None:
    start_time = _start_time_of(record.pid)
    if not start_time:
        raise RuntimeError(
            f"could not read process start time for recorded pid {record.pid}; "
            "no process was signalled"
        )
    if start_time != record.start_time:
        raise RuntimeError(
            f"process start time mismatch for recorded pid {record.pid}: "
            f"recorded {record.start_time!r}, current {start_time!r}. "
            "No process was signalled."
        )


def stop(timeout: float = 15.0) -> str:
    """Stop the orchestrator. Returns a human-readable outcome, or raises if
    the port is held by something we do not recognise."""
    record = _recorded_process()
    if record is not None:
        _require_current_identity(record)
        _require_same_process(record)
    pid = record.pid if record is not None else None
    listener_pid = _pid_on_port()
    if pid is None:
        if listener_pid is not None:
            raise RuntimeError(
                f"port {config.PORT} is held by pid {listener_pid}, but there is "
                "no recorded orchestrator PID. Stop it yourself and rerun."
            )
        PID_FILE.unlink(missing_ok=True)
        return "nothing was running"

    if listener_pid is not None and listener_pid != pid:
        raise RuntimeError(
            f"port {config.PORT} is held by pid {listener_pid}, which does not "
            f"match recorded pid {pid}. Neither process was signalled."
        )

    if not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return f"recorded pid {pid} was not running"

    command = _command_of(pid)
    if FINGERPRINT not in command:
        raise RuntimeError(
            f"recorded pid {pid} is not an "
            f"orchestrator: {command[:120]!r}. Stop it yourself and rerun."
        )

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            return f"stopped pid {pid}"
        time.sleep(0.2)

    os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    return f"force-stopped pid {pid} after it ignored SIGTERM"


def send_tick() -> dict:
    """Trigger one reconciliation pass over the authenticated control endpoint.

    The token lives in .env, which only this process reads, so a hand-typed
    `curl -H "Authorization: Bearer $GS_CONTROL_TOKEN"` sends an empty token and
    gets a 401. Going through here is what makes the command reproducible.
    """
    if not config.CONTROL_TOKEN:
        raise RuntimeError(
            "GS_CONTROL_TOKEN is unset, so the control endpoint is disabled. "
            "Add it to .env (see .env.example)."
        )
    response = httpx.post(
        f"{DASHBOARD}/api/tick",
        headers={"Authorization": f"Bearer {config.CONTROL_TOKEN}"},
        timeout=120,
    )
    if response.status_code == 401:
        raise RuntimeError(
            "the control endpoint rejected the token in .env. The running service "
            "was started with a different GS_CONTROL_TOKEN; restart it."
        )
    response.raise_for_status()
    return response.json()


def wait_for_first_tick(timeout: float = 60.0) -> int:
    """Block until the reconciler has completed a pass.

    The loop reconciles once at startup before it sleeps, so anything that
    arms a pin has to wait for that pass or race it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ticks = httpx.get(f"{DASHBOARD}/api/health", timeout=5).json()["ticks_completed"]
            if ticks >= 1:
                return ticks
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"the reconciler completed no pass within {timeout:.0f}s. "
        f"Last lines of {_relative(LOG_FILE)}:\n{_tail(LOG_FILE)}"
    )


def start(env_overrides: dict, timeout: float = 40.0) -> int:
    """Launch the orchestrator and wait for the dashboard to answer."""
    RUN_DIR.mkdir(exist_ok=True)
    env = {**os.environ, **env_overrides}
    # Pin the database explicitly so the service and the scripts that poke at
    # it can never disagree about which file they mean.
    env.setdefault("GS_DB", str(config.DB_PATH))

    with LOG_FILE.open("a") as log:
        log.write(f"\n=== started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log.flush()
        process = subprocess.Popen(
            [sys.executable, "-m", "graveyard_shift"],
            cwd=config.ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    start_time = _start_time_of(process.pid)
    if not start_time:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise RuntimeError(
            f"could not read start time for launched pid {process.pid}; "
            "the process was stopped and no PID record was written"
        )
    PID_FILE.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "port": int(env.get("GS_PORT", config.PORT)),
                "database": str(Path(env["GS_DB"]).resolve()),
                "start_time": start_time,
            }
        )
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"the orchestrator exited immediately with code {process.returncode}. "
                f"Last lines of {_relative(LOG_FILE)}:\n{_tail(LOG_FILE)}"
            )
        try:
            if httpx.get(DASHBOARD, timeout=2).status_code == 200:
                return process.pid
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    raise RuntimeError(
        f"the dashboard did not answer within {timeout:.0f}s. "
        f"Last lines of {_relative(LOG_FILE)}:\n{_tail(LOG_FILE)}"
    )


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _tail(path: Path, lines: int = 8) -> str:
    if not path.exists():
        return "(no log)"
    return "\n".join(f"    {line}" for line in path.read_text().splitlines()[-lines:])
