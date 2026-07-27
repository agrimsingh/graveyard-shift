"""Start and stop the orchestrator through a PID file.

The point of the PID file is identity. Killing whatever happens to hold port
8090 is fine until the day it is holding something else, so every signal here
goes to a process this module started and can still recognise.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from graveyard_shift import config

RUN_DIR = config.ROOT / ".run"
PID_FILE = RUN_DIR / "orchestrator.pid"
LOG_FILE = RUN_DIR / "orchestrator.log"
DASHBOARD = f"http://localhost:{config.PORT}"

# Any process we are willing to signal has this on its command line.
FINGERPRINT = "graveyard_shift"


def _command_of(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return result.stdout.strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
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
    for pid in (_recorded_pid(), _pid_on_port()):
        if pid and _alive(pid) and FINGERPRINT in _command_of(pid):
            return pid
    return None


def _recorded_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except ValueError:
        return None


def stop(timeout: float = 15.0) -> str:
    """Stop the orchestrator. Returns a human-readable outcome, or raises if
    the port is held by something we do not recognise."""
    pid = _pid_on_port()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return "nothing was running"

    command = _command_of(pid)
    if FINGERPRINT not in command:
        raise RuntimeError(
            f"port {config.PORT} is held by pid {pid}, which is not an "
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

    log = LOG_FILE.open("a")
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
    PID_FILE.write_text(str(process.pid))

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
