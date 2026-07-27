import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DEVIN_API_KEY", "test-key")
os.environ.setdefault("DEVIN_ORG_ID", "test-org")

from scripts import service


class ServiceIntegrationTests(unittest.TestCase):
    def test_real_loopback_dummy_stops_gracefully_without_sigkill(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            database = Path(directory) / "dummy.sqlite3"
            pid_file = Path(directory) / "orchestrator.pid"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "http.server",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(service.config.ROOT / "graveyard_shift"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            start_time = service._start_time_of(process.pid)
            pid_file.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "port": port,
                        "database": str(database.resolve()),
                        "start_time": start_time,
                    }
                )
            )

            try:
                # When
                with (
                    mock.patch.object(service, "PID_FILE", pid_file),
                    mock.patch.object(service.config, "PORT", port),
                    mock.patch.object(service.config, "DB_PATH", database),
                ):
                    result = service.stop(timeout=2)

                # Then
                self.assertEqual(result, f"stopped pid {process.pid}")
                self.assertFalse(pid_file.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
