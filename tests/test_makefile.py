import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class SetupTargetTests(unittest.TestCase):
    def test_setup_bootstraps_venv_and_installs_requirements(self) -> None:
        # Given
        command = ["make", "--dry-run", "--always-make", "setup"]

        # When
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

        # Then
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("python3 -m venv .venv", result.stdout)
        # requirements-dev.txt includes requirements.txt, so one install covers
        # both running the orchestrator and running these tests.
        self.assertIn(".venv/bin/python -m pip install -r requirements-dev.txt", result.stdout)

    def test_check_runs_all_three_credential_free_layers(self) -> None:
        """Each layer catches a different class of bug, so `make check` has to
        run all of them or the coverage quietly stops being enforced."""
        # Given
        command = ["make", "--dry-run", "--always-make", "check"]

        # When
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

        # Then
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("-m pytest tests/", "scripts/verify_convergence.py",
                         "scripts/simulate.py"):
            self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
