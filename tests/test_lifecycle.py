import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DEVIN_API_KEY", "test-key")
os.environ.setdefault("DEVIN_ORG_ID", "test-org")

from graveyard_shift import config, controller, devin, store


class DevinTerminationTests(unittest.TestCase):
    def test_terminate_session_uses_the_organization_delete_endpoint(self) -> None:
        # Given
        stopped = {"session_id": "devin-123", "status": "exit"}

        # When
        with mock.patch.object(devin, "_request", return_value=stopped) as request:
            result = devin.terminate_session("devin-123")

        # Then
        self.assertEqual(stopped, result)
        request.assert_called_once_with("DELETE", "/sessions/devin-123")
        self.assertTrue(devin.is_stopped(result))
        self.assertFalse(devin.is_stopped({"status": "suspended"}))

    def test_stop_session_polls_until_delete_reaches_exit(self) -> None:
        # Given
        snapshots = [
            {"session_id": "devin-123", "status": "running"},
            {"session_id": "devin-123", "status": "running"},
            {"session_id": "devin-123", "status": "exit"},
        ]

        # When
        with (
            mock.patch.object(devin, "get_session", side_effect=snapshots) as get_session,
            mock.patch.object(devin, "terminate_session") as terminate,
            mock.patch.object(devin.time, "sleep") as sleep,
        ):
            devin.stop_session("devin-123")

        # Then
        terminate.assert_called_once_with("devin-123")
        self.assertEqual(3, get_session.call_count)
        sleep.assert_called_once_with(0.25)

    def test_stop_session_times_out_after_a_bounded_number_of_polls(self) -> None:
        # Given
        running = {"session_id": "devin-123", "status": "running"}

        # When
        with (
            mock.patch.object(devin, "get_session", return_value=running) as get_session,
            mock.patch.object(devin, "terminate_session"),
            mock.patch.object(devin.time, "sleep") as sleep,
        ):
            with self.assertRaises(devin.SessionStillRunningError):
                devin.stop_session("devin-123")

        # Then
        self.assertEqual(5, get_session.call_count)
        self.assertEqual([mock.call(0.25), mock.call(0.5), mock.call(1.0)], sleep.call_args_list)


class TerminalTransitionTests(unittest.TestCase):
    def test_missing_devin_pr_url_escalates_instead_of_stranding(self) -> None:
        # Given
        run = {"session_id": "devin-123", "pr_url": None, "updated_at": 0}
        session = {"status": "running", "pull_requests": [{}]}

        # When
        with mock.patch.object(controller, "escalate") as escalate:
            controller.step_remediating(mock.sentinel.conn, run, mock.sentinel.pin, session)

        # Then
        escalate.assert_called_once_with(
            mock.sentinel.conn,
            run,
            mock.sentinel.pin,
            "Devin returned an invalid pull request",
        )

    def test_invalid_devin_pr_url_escalates_before_persisting(self) -> None:
        # Given
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(config, "DB_PATH", Path(directory) / "state.db"),
            mock.patch.object(config, "FORK", "agrimsingh/superset"),
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                run_id = store.create_run(
                    conn, pin["id"], "devin-123", "https://app.devin.ai/sessions/123"
                )
                conn.execute(
                    "UPDATE runs SET state = ? WHERE id = ?",
                    (store.REMEDIATING, run_id),
                )

            # When
            with (
                mock.patch.object(controller.devin, "stop_session") as stop_session,
                mock.patch.object(controller.gh, "set_labels"),
                mock.patch.object(controller.gh, "comment"),
            ):
                with store.db() as conn:
                    run = conn.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    pin = conn.execute(
                        "SELECT * FROM pins WHERE id = ?", (pin["id"],)
                    ).fetchone()
                    controller.step_remediating(
                        conn,
                        run,
                        pin,
                        {
                            "status": "running",
                            "pull_requests": [
                                {"pr_url": "https://github.com/evil/repo/pull/1"}
                            ],
                        },
                    )

            # Then
            stop_session.assert_called_once_with("devin-123")
            with store.db() as conn:
                persisted = conn.execute(
                    "SELECT state, pr_url FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
            self.assertEqual(store.ESCALATED, persisted["state"])
            self.assertIsNone(persisted["pr_url"])

    def test_non_string_devin_pr_urls_stop_escalate_and_never_persist(self) -> None:
        for index, pr_url in enumerate((123, b"bytes", None, object())):
            with (
                self.subTest(pr_url=repr(pr_url)),
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(config, "DB_PATH", Path(directory) / "state.db"),
                mock.patch.object(config, "FORK", "agrimsingh/superset"),
            ):
                with store.db() as conn:
                    pin = store.upsert_pin(
                        conn, f"package-{index}", "/", "reason", f"hash-{index}"
                    )
                    run_id = store.create_run(
                        conn,
                        pin["id"],
                        f"devin-{index}",
                        f"https://app.devin.ai/sessions/{index}",
                    )
                    conn.execute(
                        "UPDATE runs SET state = ? WHERE id = ?",
                        (store.REMEDIATING, run_id),
                    )

                with (
                    mock.patch.object(controller.devin, "stop_session") as stop_session,
                    mock.patch.object(controller.gh, "set_labels"),
                    mock.patch.object(controller.gh, "comment"),
                ):
                    with store.db() as conn:
                        run = conn.execute(
                            "SELECT * FROM runs WHERE id = ?", (run_id,)
                        ).fetchone()
                        pin = conn.execute(
                            "SELECT * FROM pins WHERE id = ?", (pin["id"],)
                        ).fetchone()
                        controller.step_remediating(
                            conn,
                            run,
                            pin,
                            {
                                "status": "running",
                                "pull_requests": [{"pr_url": pr_url}],
                            },
                        )

                stop_session.assert_called_once_with(f"devin-{index}")
                with store.db() as conn:
                    persisted = conn.execute(
                        "SELECT state, pr_url FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                self.assertEqual(store.ESCALATED, persisted["state"])
                self.assertIsNone(persisted["pr_url"])

    def test_escalation_stops_and_verifies_the_remote_session_first(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                run_id = store.create_run(
                    conn, pin["id"], "devin-123", "https://app.devin.ai/sessions/123"
                )

            # When
            with (
                mock.patch.object(
                    controller.devin,
                    "get_session",
                    side_effect=[
                        {"session_id": "devin-123", "status": "running"},
                        {"session_id": "devin-123", "status": "exit"},
                    ],
                ) as get_session,
                mock.patch.object(
                    controller.devin,
                    "terminate_session",
                    return_value={"session_id": "devin-123", "status": "exit"},
                ) as terminate,
                mock.patch.object(controller.gh, "set_labels"),
                mock.patch.object(controller.gh, "comment"),
            ):
                with store.db() as conn:
                    run = conn.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    pin = conn.execute(
                        "SELECT * FROM pins WHERE id = ?", (run["pin_id"],)
                    ).fetchone()
                    controller.escalate(conn, run, pin, "needs a human")

            # Then
            terminate.assert_called_once_with("devin-123")
            self.assertEqual(2, get_session.call_count)
            with store.db() as conn:
                state = conn.execute(
                    "SELECT state FROM runs WHERE id = ?", (run_id,)
                ).fetchone()[0]
            self.assertEqual(store.ESCALATED, state)

    def test_escalation_completes_and_warns_when_remote_stop_is_not_verified(self) -> None:
        """Escalation is the fallback every other failure path falls into, so a
        stop it cannot confirm must not strand the run. The unconfirmed session
        becomes something the human is warned about instead."""
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                run_id = store.create_run(
                    conn, pin["id"], "devin-123", "https://app.devin.ai/sessions/123"
                )

            # When
            with (
                mock.patch.object(
                    controller.devin,
                    "get_session",
                    return_value={"session_id": "devin-123", "status": "running"},
                ),
                mock.patch.object(
                    controller.devin,
                    "terminate_session",
                    return_value={"session_id": "devin-123", "status": "running"},
                ),
                mock.patch.object(controller.devin.time, "sleep"),
                mock.patch.object(controller.gh, "set_labels") as set_labels,
                mock.patch.object(controller.gh, "comment") as comment,
            ):
                with store.db() as conn:
                    run = conn.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    pin = conn.execute(
                        "SELECT * FROM pins WHERE id = ?", (run["pin_id"],)
                    ).fetchone()
                    controller.escalate(conn, run, pin, "needs a human")

            # Then
            set_labels.assert_called_once_with(pin["issue_number"], ["pin-audit", "needs-human"])
            self.assertIn("could not be confirmed stopped", comment.call_args.args[1])
            with store.db() as conn:
                state = conn.execute(
                    "SELECT state FROM runs WHERE id = ?", (run_id,)
                ).fetchone()[0]
                kinds = [
                    row["kind"]
                    for row in conn.execute("SELECT kind FROM events WHERE run_id = ?", (run_id,))
                ]
            self.assertEqual(store.ESCALATED, state)
            self.assertIn("stop_failed", kinds)

    def test_green_waits_for_a_confirmed_stop_then_escalates_at_the_limit(self) -> None:
        """A session that keeps working can still open a pull request, so `green`
        may only be recorded once the stop is confirmed. Retrying forever would
        hold the slot, so exhausting the attempts hands it to a human."""
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                run_id = store.create_run(
                    conn, pin["id"], "devin-123", "https://app.devin.ai/sessions/123"
                )
                run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                store.transition(conn, run, store.REMEDIATING)
                run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                store.transition(conn, run, store.AWAITING_CI,
                                 pr_url="https://github.com/o/r/pull/1")

            observed = []
            # When: CI is green but the session never confirms it stopped.
            with (
                mock.patch.object(
                    controller.devin, "stop_session",
                    side_effect=devin.SessionStillRunningError("devin-123", "running"),
                ),
                mock.patch.object(controller.gh, "pr_checks", return_value={
                    "conclusion": "success", "head_sha": "sha1",
                    "has_checks": True, "failures": [],
                }),
                mock.patch.object(controller.gh, "set_labels"),
                mock.patch.object(controller.gh, "comment"),
            ):
                for _ in range(config.STOP_ATTEMPT_LIMIT):
                    with store.db() as conn:
                        run = conn.execute(
                            "SELECT * FROM runs WHERE id = ?", (run_id,)
                        ).fetchone()
                        pin = conn.execute(
                            "SELECT * FROM pins WHERE id = ?", (run["pin_id"],)
                        ).fetchone()
                        controller.step_awaiting_ci(conn, run, pin, {"status": "running"})
                        observed.append(conn.execute(
                            "SELECT state FROM runs WHERE id = ?", (run_id,)
                        ).fetchone()[0])

        # Then: never green, and the last attempt hands it over rather than looping.
        self.assertNotIn(store.GREEN, observed)
        self.assertEqual(store.ESCALATED, observed[-1])
        self.assertEqual([store.AWAITING_CI] * (config.STOP_ATTEMPT_LIMIT - 1), observed[:-1])


class ControllerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        controller._LABELS_READY = False

    def test_label_bootstrap_retries_on_the_next_tick(self) -> None:
        # Given
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(config, "DB_PATH", Path(directory) / "state.db"),
            mock.patch.object(
                controller.gh,
                "ensure_labels",
                side_effect=[RuntimeError("GitHub unavailable"), None],
            ) as ensure_labels,
            mock.patch.object(controller, "sync_pins"),
            mock.patch.object(controller, "reconcile_runs"),
            mock.patch.object(controller, "admit"),
        ):
            # When
            with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
                controller.tick()
            controller.tick()

        # Then
        self.assertEqual(2, ensure_labels.call_count)
        self.assertTrue(controller._LABELS_READY)

    def test_status_records_degraded_sync_then_clears_after_a_clean_tick(self) -> None:
        # Given
        controller._LABELS_READY = True
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(config, "DB_PATH", Path(directory) / "state.db"),
            mock.patch.object(
                controller, "sync_pins", side_effect=[RuntimeError("sync failed"), None]
            ),
            mock.patch.object(controller, "reconcile_runs"),
            mock.patch.object(controller, "admit"),
        ):
            # When
            controller.tick()
            degraded = controller.status()
            controller.tick()
            recovered = controller.status()

        # Then
        self.assertIn("RuntimeError: sync failed", degraded["last_tick_error"])
        self.assertIsNotNone(degraded["last_tick_error_at"])
        self.assertIsNotNone(degraded["last_tick_completed_at"])
        self.assertIsNone(recovered["last_tick_error"])
        self.assertIsNone(recovered["last_tick_error_at"])


class MetricsTruthfulnessTests(unittest.TestCase):
    def test_speed_metrics_are_null_until_a_run_has_reached_those_states(self) -> None:
        """Reporting nothing and reporting zero are different claims."""
        # Given
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                # When
                metrics = store.metrics(conn)

        # Then
        self.assertIsNone(metrics["green_without_repair_round"])
        self.assertIsNone(metrics["median_trigger_to_pr_s"])
        self.assertIsNone(metrics["median_trigger_to_green_s"])

    def test_speed_metrics_measure_from_the_trigger_to_each_state(self) -> None:
        """Throughput is the question a team asks of an autonomous system: how
        long until it hands me something reviewable, and how often does it get
        there without a second attempt."""
        # Given: a run that reached CI after 60s, recorded green at 100s, then
        # recorded green again at 300s because the first verdict did not hold.
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            config, "DB_PATH", Path(directory) / "state.db"
        ):
            with store.db() as conn:
                pin = store.upsert_pin(conn, "package", "/", "reason", "hash")
                run_id = store.create_run(
                    conn, pin["id"], "devin-1", "https://app.devin.ai/sessions/1"
                )
                started = conn.execute(
                    "SELECT created_at FROM runs WHERE id = ?", (run_id,)
                ).fetchone()[0]
                conn.execute("UPDATE runs SET state = ?, attempts = 0 WHERE id = ?",
                             (store.GREEN, run_id))
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                for kind, offset in (
                    (f"state:{store.AWAITING_CI}", 60),
                    (f"state:{store.GREEN}", 100),
                    (f"state:{store.GREEN}", 300),
                ):
                    conn.execute(
                        "INSERT INTO events (run_id, at, kind, detail) VALUES (?, ?, ?, '')",
                        (run_id, started + offset, kind),
                    )

                # When
                metrics = store.metrics(conn)

        # Then
        self.assertEqual(60, metrics["median_trigger_to_pr_s"])
        # 300, not 100: a run that recorded green twice had not settled the first
        # time, and reporting the earlier claim would understate it.
        self.assertEqual(300, metrics["median_trigger_to_green_s"])
        self.assertEqual("1/1", metrics["green_without_repair_round"])


if __name__ == "__main__":
    unittest.main()
