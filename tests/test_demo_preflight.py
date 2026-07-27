import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("DEVIN_API_KEY", "test-key")
os.environ.setdefault("DEVIN_ORG_ID", "test-org")

from graveyard_shift import store
from scripts import demo_preflight


class FakeDevin:
    def __init__(self, snapshots: list[dict[str, str]]) -> None:
        self.snapshots = snapshots
        self.snapshot_index = 0
        self.calls: list[tuple[str, str]] = []

    def get_session(self, session_id: str) -> dict[str, str]:
        self.calls.append(("get", session_id))
        index = min(self.snapshot_index, len(self.snapshots) - 1)
        self.snapshot_index += 1
        return self.snapshots[index]

    def terminate_session(self, session_id: str) -> dict[str, str]:
        self.calls.append(("terminate", session_id))
        return {"session_id": session_id}

    @staticmethod
    def is_stopped(session: dict[str, str]) -> bool:
        return session["status"] == "exit"


class DemoCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(store.SCHEMA)
        pin_id = self.conn.execute(
            "INSERT INTO pins (dependency, directory, reason, entry_hash)"
            " VALUES (?, ?, ?, ?)",
            (demo_preflight.DEMO_PIN, "/", "demo", "hash"),
        ).lastrowid
        self.pin_id = int(pin_id)
        self.baseline_id = self._insert_run("baseline", store.GREEN)
        self.demo_run_id = self._insert_run("demo-session", store.CLASSIFYING)
        self.conn.execute(
            "INSERT INTO events (run_id, at, kind, detail) VALUES (?, 0, 'run_created', '')",
            (self.demo_run_id,),
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_run(self, session_id: str, state: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs"
            " (pin_id, session_id, session_url, state, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 0, 0)",
            (
                self.pin_id,
                session_id,
                f"https://app.devin.ai/sessions/{session_id}",
                state,
            ),
        )
        return int(cursor.lastrowid)

    def test_cleanup_terminates_and_verifies_active_session_before_deleting_tracking(self) -> None:
        # Given
        client = FakeDevin(
            [
                {"session_id": "demo-session", "status": "running"},
                {"session_id": "demo-session", "status": "exit"},
            ]
        )

        # When
        with mock.patch.object(demo_preflight, "devin", client, create=True):
            detail = demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertEqual(
            client.calls,
            [
                ("get", "demo-session"),
                ("terminate", "demo-session"),
                ("get", "demo-session"),
            ],
        )
        self.assertIn("terminated", detail or "")
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM runs WHERE id = ?", (self.demo_run_id,)
            ).fetchone()
        )

    def test_cleanup_refuses_mismatched_remote_session_and_keeps_tracking(self) -> None:
        # Given
        client = FakeDevin([{"session_id": "different-session", "status": "running"}])

        # When
        with (
            mock.patch.object(demo_preflight, "devin", client, create=True),
            self.assertRaisesRegex(demo_preflight.NotReady, "identity mismatch"),
        ):
            demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertEqual(client.calls, [("get", "demo-session")])
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT id FROM runs WHERE id = ?", (self.demo_run_id,)
            ).fetchone()
        )

    def test_cleanup_keeps_tracking_when_termination_is_not_verified(self) -> None:
        # Given
        client = FakeDevin(
            [
                {"session_id": "demo-session", "status": "running"},
                {"session_id": "demo-session", "status": "suspended"},
            ]
        )

        # When
        with (
            mock.patch.object(demo_preflight, "devin", client, create=True),
            mock.patch.object(demo_preflight, "TERMINATION_TIMEOUT_SECONDS", 0),
            self.assertRaisesRegex(demo_preflight.NotReady, "did not stop"),
        ):
            demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT id FROM runs WHERE id = ?", (self.demo_run_id,)
            ).fetchone()
        )

    def test_cleanup_skips_termination_only_when_get_proves_remote_exit(self) -> None:
        # Given
        client = FakeDevin(
            [
                {"session_id": "demo-session", "status": "exit"},
                {"session_id": "demo-session", "status": "exit"},
            ]
        )

        # When
        with mock.patch.object(demo_preflight, "devin", client, create=True):
            demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertEqual(
            client.calls,
            [
                ("get", "demo-session"),
            ],
        )

    def test_cleanup_terminates_locally_terminal_run_still_running_remotely(self) -> None:
        # Given
        self.conn.execute(
            "UPDATE runs SET state = ? WHERE id = ?",
            (store.ESCALATED, self.demo_run_id),
        )
        client = FakeDevin(
            [
                {"session_id": "demo-session", "status": "running"},
                {"session_id": "demo-session", "status": "exit"},
            ]
        )

        # When
        with mock.patch.object(demo_preflight, "devin", client, create=True):
            demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertEqual(
            client.calls,
            [
                ("get", "demo-session"),
                ("terminate", "demo-session"),
                ("get", "demo-session"),
            ],
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM runs WHERE id = ?", (self.demo_run_id,)
            ).fetchone()
        )

    def test_cleanup_refuses_malformed_session_snapshot_and_keeps_tracking(self) -> None:
        # Given
        client = FakeDevin(
            [
                {"session_id": "demo-session", "status": "running"},
                {"session_id": "demo-session"},
            ]
        )

        # When
        with (
            mock.patch.object(demo_preflight, "devin", client, create=True),
            self.assertRaisesRegex(demo_preflight.NotReady, "status"),
        ):
            demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT id FROM runs WHERE id = ?", (self.demo_run_id,)
            ).fetchone()
        )

    def test_cleanup_polls_until_asynchronous_termination_reaches_exit(self) -> None:
        # Given
        client = FakeDevin(
            [
                {"session_id": "demo-session", "status": "running"},
                {"session_id": "demo-session", "status": "running"},
                {"session_id": "demo-session", "status": "exit"},
            ]
        )

        # When
        with (
            mock.patch.object(demo_preflight, "devin", client, create=True),
            mock.patch.object(demo_preflight.time, "sleep") as sleep,
        ):
            demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertEqual(
            client.calls,
            [
                ("get", "demo-session"),
                ("terminate", "demo-session"),
                ("get", "demo-session"),
                ("get", "demo-session"),
            ],
        )
        sleep.assert_called_once()

    def test_cleanup_is_idempotent_when_demo_pin_is_absent(self) -> None:
        # Given
        self.conn.execute("DELETE FROM events")
        self.conn.execute("DELETE FROM runs")
        self.conn.execute("DELETE FROM pins")

        # When
        detail = demo_preflight.discard_rehearsal_run(self.conn)

        # Then
        self.assertIsNone(detail)

    def test_stop_command_is_idempotent_with_empty_database(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "empty.sqlite3"
            output = io.StringIO()

            # When
            with (
                mock.patch.object(demo_preflight.config, "DB_PATH", database),
                mock.patch.object(
                    demo_preflight.service,
                    "stop",
                    return_value="nothing was running",
                ),
                mock.patch.object(
                    demo_preflight.sys,
                    "argv",
                    ["demo_preflight.py", "--stop"],
                ),
                redirect_stdout(output),
            ):
                demo_preflight.main()

            # Then
            self.assertEqual(
                output.getvalue().splitlines(),
                ["nothing was running", "no rehearsal run to discard"],
            )


class AdmissionClaimGuardTests(unittest.TestCase):
    """A claim never expires, so one stranded by preflight's own restart would
    silently consume the demo's single concurrency slot and make the on-camera
    tick a no-op. Checking once before the restart cannot catch that."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = Path(self.directory.name) / "state.db"
        patch = mock.patch.object(demo_preflight.config, "DB_PATH", database)
        patch.start()
        self.addCleanup(patch.stop)
        with store.db() as conn:
            self.pin = store.upsert_pin(
                conn, demo_preflight.DEMO_PIN, "/", "demo", "hash"
            )

    def _strand_claim(self) -> str:
        claim = store.claim_pin(self.pin["id"])
        self.assertIsNotNone(claim)
        return claim

    def test_claim_stranded_by_the_restart_is_caught_before_ready(self) -> None:
        # Given: stopping the old service interrupts a launch mid-claim, which is
        # precisely the window the earlier check has already passed through.
        def stop_and_strand() -> str:
            self._strand_claim()
            return "stopped"

        # When
        with (
            mock.patch.object(demo_preflight.config, "CONTROL_TOKEN", "token"),
            # Narrow the up-front gate to its claim check so this test is about
            # ordering rather than about the recorded run sheet's totals.
            mock.patch.object(
                demo_preflight, "check_converged", demo_preflight.check_no_claims
            ),
            mock.patch.object(
                demo_preflight.service, "stop", side_effect=stop_and_strand
            ),
            mock.patch.object(demo_preflight.service, "start") as start,
        ):
            with self.assertRaises(demo_preflight.NotReady) as caught:
                demo_preflight.preflight()

        # Then: it refuses, and never gets as far as restarting the service.
        self.assertIn("admission claim", str(caught.exception))
        start.assert_not_called()

    def test_recovery_targets_the_inspected_claim_by_token(self) -> None:
        # Given
        token = self._strand_claim()

        # When
        with store.db() as conn:
            with self.assertRaises(demo_preflight.NotReady) as caught:
                demo_preflight.check_no_claims(conn)

        # Then: clearing the table would release claims whose Devin sessions are
        # still unaccounted for, so those pins would be launched a second time.
        message = str(caught.exception)
        self.assertIn(f"WHERE token = '{token}'", message)
        self.assertNotIn("DELETE FROM admission_claims\"", message)


if __name__ == "__main__":
    unittest.main()
