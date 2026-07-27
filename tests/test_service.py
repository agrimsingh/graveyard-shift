import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DEVIN_API_KEY", "test-key")
os.environ.setdefault("DEVIN_ORG_ID", "test-org")

from scripts import service


START_TIME = "Mon Jul 27 12:00:00 2026"


def write_record(path: Path, pid: int = 410) -> None:
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "port": service.config.PORT,
                "database": str(service.config.DB_PATH.resolve()),
                "start_time": START_TIME,
            }
        )
    )


class StopServiceTests(unittest.TestCase):
    def test_start_records_pid_port_and_database_launch_identity(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / ".run"
            pid_file = run_dir / "orchestrator.pid"
            log_file = run_dir / "orchestrator.log"
            database = Path(directory) / "demo.sqlite3"
            process = mock.Mock(pid=410)
            process.poll.return_value = None
            response = mock.Mock(status_code=200)

            # When
            with (
                mock.patch.object(service, "RUN_DIR", run_dir),
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service, "LOG_FILE", log_file),
                mock.patch.object(
                    service,
                    "_start_time_of",
                    return_value=START_TIME,
                    create=True,
                ),
                mock.patch.object(
                    service.subprocess,
                    "Popen",
                    new=lambda *args, **kwargs: process,
                ),
                mock.patch.object(service.httpx, "get", return_value=response),
            ):
                pid = service.start(
                    {"GS_DB": str(database), "GS_PORT": "9001"},
                    timeout=0.1,
                )

            # Then
            self.assertEqual(pid, 410)
            self.assertEqual(
                json.loads(pid_file.read_text()),
                {
                    "pid": 410,
                    "port": 9001,
                    "database": str(database.resolve()),
                    "start_time": START_TIME,
                },
            )

    def test_alive_treats_zombie_child_as_exited_and_reaps_it(self) -> None:
        # Given / When
        with (
            mock.patch.object(service.os, "kill"),
            mock.patch.object(service, "_process_state", return_value="Z", create=True),
            mock.patch.object(service.os, "waitpid", return_value=(410, 0)) as waitpid,
        ):
            alive = service._alive(410)

        # Then
        self.assertFalse(alive)
        waitpid.assert_called_once_with(410, service.os.WNOHANG)

    def test_start_stops_untracked_process_when_start_time_is_unreadable(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / ".run"
            pid_file = run_dir / "orchestrator.pid"
            log_file = run_dir / "orchestrator.log"
            process = mock.Mock(pid=410)

            # When
            with (
                mock.patch.object(service, "RUN_DIR", run_dir),
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service, "LOG_FILE", log_file),
                mock.patch.object(service, "_start_time_of", return_value=""),
                mock.patch.object(
                    service.subprocess,
                    "Popen",
                    new=lambda *args, **kwargs: process,
                ),
                self.assertRaisesRegex(RuntimeError, "no PID record was written"),
            ):
                service.start({})

            # Then
            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=5)
            self.assertFalse(pid_file.exists())

    def test_stop_signals_recorded_pid_when_no_process_listens_on_port(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            write_record(pid_file)

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service, "_pid_on_port", return_value=None),
                mock.patch.object(
                    service,
                    "_start_time_of",
                    return_value=START_TIME,
                    create=True,
                ),
                mock.patch.object(service, "_command_of", return_value="python -m graveyard_shift"),
                mock.patch.object(service, "_alive", side_effect=[True, False]),
                mock.patch.object(service.os, "kill") as kill,
            ):
                result = service.stop()

            # Then
            kill.assert_called_once_with(410, service.signal.SIGTERM)
            self.assertEqual(result, "stopped pid 410")
            self.assertFalse(pid_file.exists())

    def test_stop_refuses_mismatched_port_listener_without_signalling_either_pid(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            write_record(pid_file)

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service, "_pid_on_port", return_value=911),
                mock.patch.object(
                    service,
                    "_start_time_of",
                    return_value=START_TIME,
                    create=True,
                ),
                mock.patch.object(service, "_command_of", return_value="python -m graveyard_shift"),
                mock.patch.object(service, "_alive", return_value=True),
                mock.patch.object(service.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "pid 911.*recorded pid 410"):
                    service.stop()

            # Then
            kill.assert_not_called()
            self.assertTrue(pid_file.exists())

    def test_stop_refuses_listener_when_there_is_no_recorded_pid(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service, "_pid_on_port", return_value=911),
                mock.patch.object(service.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "pid 911.*no recorded orchestrator PID"):
                    service.stop()

            # Then
            kill.assert_not_called()

    def test_stop_force_kills_only_after_recorded_pid_ignores_sigterm(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            write_record(pid_file)

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service, "_pid_on_port", return_value=410),
                mock.patch.object(
                    service,
                    "_start_time_of",
                    return_value=START_TIME,
                    create=True,
                ),
                mock.patch.object(service, "_command_of", return_value="python -m graveyard_shift"),
                mock.patch.object(service, "_alive", return_value=True),
                mock.patch.object(service.os, "kill") as kill,
            ):
                result = service.stop(timeout=0)

            # Then
            self.assertEqual(
                kill.call_args_list,
                [
                    mock.call(410, service.signal.SIGTERM),
                    mock.call(410, service.signal.SIGKILL),
                ],
            )
            self.assertIn("force-stopped pid 410", result)
            self.assertFalse(pid_file.exists())

    def test_stop_refuses_record_from_different_port_and_database(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            write_record(pid_file)
            other_database = Path(directory) / "other.sqlite3"

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service.config, "PORT", service.config.PORT + 1),
                mock.patch.object(service.config, "DB_PATH", other_database),
                mock.patch.object(service.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "launch identity mismatch"):
                    service.stop()

            # Then
            kill.assert_not_called()
            self.assertTrue(pid_file.exists())

    def test_stop_refuses_reused_pid_with_different_process_start_time(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            write_record(pid_file)

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(
                    service,
                    "_start_time_of",
                    return_value="Mon Jul 27 12:05:00 2026",
                    create=True,
                ),
                mock.patch.object(service, "_pid_on_port") as pid_on_port,
                mock.patch.object(service.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "process start time mismatch"):
                    service.stop()

            # Then
            pid_on_port.assert_not_called()
            kill.assert_not_called()
            self.assertTrue(pid_file.exists())

    def test_stop_refuses_when_process_start_time_is_unreadable(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            write_record(pid_file)

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(
                    service,
                    "_start_time_of",
                    return_value="",
                    create=True,
                ),
                mock.patch.object(service.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "could not read process start time"):
                    service.stop()

            # Then
            kill.assert_not_called()
            self.assertTrue(pid_file.exists())

    def test_stop_refuses_legacy_pid_only_record(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "orchestrator.pid"
            pid_file.write_text("410")

            # When
            with (
                mock.patch.object(service, "PID_FILE", pid_file),
                mock.patch.object(service.os, "kill") as kill,
            ):
                with self.assertRaisesRegex(RuntimeError, "legacy PID file"):
                    service.stop()

            # Then
            kill.assert_not_called()
            self.assertTrue(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
