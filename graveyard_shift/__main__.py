"""Entrypoint: FastAPI dashboard + the scheduled reconciler loop."""

import logging
import threading
import time

import uvicorn

from . import config, controller, gh
from .web import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("graveyard")


def loop() -> None:
    gh.ensure_labels()
    while True:
        try:
            controller.tick()
        except Exception:
            logger.exception("tick failed")
        time.sleep(config.TICK_SECONDS)


def main() -> None:
    threading.Thread(target=loop, daemon=True, name="reconciler").start()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
