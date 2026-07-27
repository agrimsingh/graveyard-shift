import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class CredentialFreeReplayTests(unittest.TestCase):
    def test_replay_stops_sessions_without_real_http_fallback(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "httpx.py"
            shim.write_text(
                "def request(*args, **kwargs):\n"
                "    raise AssertionError('simulation attempted real HTTP')\n"
            )
            env = {
                **os.environ,
                "DEVIN_API_KEY": "must-not-be-used",
                "DEVIN_ORG_ID": "must-not-be-used",
                "PYTHONPATH": os.pathsep.join(
                    [directory, os.environ.get("PYTHONPATH", "")]
                ),
            }

            # When
            result = subprocess.run(
                [sys.executable, "scripts/simulate.py"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

        # Then
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "PASS  every terminal run stopped its fake Devin session",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
