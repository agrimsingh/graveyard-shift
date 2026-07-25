import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

DEVIN_API_KEY = os.environ["DEVIN_API_KEY"]
DEVIN_ORG_ID = os.environ["DEVIN_ORG_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Repository allowlist: the controller refuses to operate anywhere else.
FORK = os.environ.get("GS_FORK", "agrimsingh/superset")
DEFAULT_BRANCH = os.environ.get("GS_DEFAULT_BRANCH", "master")

DB_PATH = Path(os.environ.get("GS_DB", ROOT / "graveyard.sqlite3"))

MAX_CONCURRENT_RUNS = int(os.environ.get("GS_MAX_CONCURRENT", "2"))
MAX_ACU_PER_RUN = int(os.environ.get("GS_MAX_ACU", "15"))
RETRY_LIMIT = int(os.environ.get("GS_RETRY_LIMIT", "1"))
REMEDIATION_GRACE_SECONDS = int(os.environ.get("GS_REMEDIATION_GRACE", "300"))
RECHECK_DAYS = int(os.environ.get("GS_RECHECK_DAYS", "14"))
CONFIDENCE_THRESHOLD = float(os.environ.get("GS_CONFIDENCE", "0.6"))
TICK_SECONDS = int(os.environ.get("GS_TICK_SECONDS", "60"))

# Pins the controller may admit. Empty means all discovered pins are eligible.
PIN_ALLOWLIST = [
    p for p in os.environ.get("GS_PIN_ALLOWLIST", "").split(",") if p.strip()
]
