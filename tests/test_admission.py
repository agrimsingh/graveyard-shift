import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DEVIN_API_KEY", "test-key")
os.environ.setdefault("DEVIN_ORG_ID", "test-org")

from graveyard_shift import config, controller, store


class AdmissionClaimTests(unittest.TestCase):
    def test_metrics_expose_claim_count_and_oldest_age(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                conn.execute(
                    "INSERT INTO admission_claims"
                    " (pin_id, group_key, token, claimed_at) VALUES (?, ?, ?, ?)",
                    (pin["id"], store.group_key(pin), "claim-token", 40),
                )

            # When
            with mock.patch.object(store.time, "time", return_value=100):
                with store.db() as conn:
                    metrics = store.metrics(conn)

            # Then
            self.assertEqual(1, metrics["admission_claims_in_flight"])
            self.assertEqual(60, metrics["oldest_admission_claim_age_s"])

    def test_old_claim_remains_authoritative_until_explicit_recovery(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                pin_id = pin["id"]
                conn.execute(
                    "INSERT INTO admission_claims"
                    " (pin_id, group_key, token, claimed_at) VALUES (?, ?, ?, ?)",
                    (pin_id, store.group_key(pin), "original-token", 0),
                )

            # When
            replacement = store.claim_pin(pin_id)

            # Then
            self.assertIsNone(replacement)
            with store.db() as conn:
                claim = conn.execute(
                    "SELECT token FROM admission_claims WHERE pin_id = ?", (pin_id,)
                ).fetchone()
            self.assertEqual("original-token", claim["token"])

    def test_only_one_concurrent_caller_claims_a_due_pin(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ), mock.patch.object(config, "MAX_CONCURRENT_RUNS", 1):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                pin_id = pin["id"]
            barrier = threading.Barrier(3)
            claims: list[str | None] = []

            def claim() -> None:
                barrier.wait()
                claims.append(store.claim_pin(pin_id))

            first = threading.Thread(target=claim)
            second = threading.Thread(target=claim)
            first.start()
            second.start()

            # When
            barrier.wait()
            first.join()
            second.join()

            # Then
            self.assertEqual(1, len([claim for claim in claims if claim is not None]))

    def test_failed_launch_releases_the_claim_and_records_the_error(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                pin_id = pin["id"]

            # When
            with (
                mock.patch.object(controller.gh, "create_issue", return_value=7),
                mock.patch.object(
                    controller.devin,
                    "create_session",
                    side_effect=RuntimeError("Devin unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "Devin unavailable"):
                    controller.admit()

            # Then
            with store.db() as conn:
                claim_count = conn.execute(
                    "SELECT COUNT(*) FROM admission_claims WHERE pin_id = ?", (pin_id,)
                ).fetchone()[0]
                event = conn.execute(
                    "SELECT kind, detail FROM events ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(0, claim_count)
            self.assertEqual("launch_failed", event["kind"])
            self.assertIn("Devin unavailable", event["detail"])

    def test_persistence_failure_stops_the_created_session_and_preserves_the_error(self) -> None:
        # Given
        persistence_error = sqlite3.OperationalError("database unavailable")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                pin_id = pin["id"]

            # When
            with (
                mock.patch.object(controller.gh, "create_issue", return_value=7),
                mock.patch.object(
                    controller.devin,
                    "create_session",
                    return_value={
                        "session_id": "devin-123",
                        "url": "https://app.devin.ai/sessions/123",
                    },
                ),
                mock.patch.object(
                    controller.store, "finish_claim", side_effect=persistence_error
                ),
                mock.patch.object(
                    controller.devin,
                    "stop_session",
                    side_effect=RuntimeError("cleanup failed"),
                ) as stop_session,
                self.assertLogs("graveyard", level="ERROR") as logs,
            ):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    controller.admit()

            # Then
            self.assertIs(persistence_error, caught.exception)
            stop_session.assert_called_once_with("devin-123")
            self.assertIn("failed to stop untracked Devin session", "\n".join(logs.output))
            with store.db() as conn:
                claim_count = conn.execute(
                    "SELECT COUNT(*) FROM admission_claims WHERE pin_id = ?", (pin_id,)
                ).fetchone()[0]
                event = conn.execute(
                    "SELECT kind, detail FROM events ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(0, claim_count)
            self.assertEqual("launch_failed", event["kind"])
            self.assertIn("OperationalError: database unavailable", event["detail"])


if __name__ == "__main__":
    unittest.main()
