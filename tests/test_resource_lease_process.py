"""Actual direct-child runner qualification for explicit held-open stdin."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Timer
import unittest
from unittest import mock

from resourcelease import process as commands
from resourcelease.model import LeaseError, QuarantineError


class TrackedInput:
    def __init__(self, stream, child, *, fail_close=False):
        self.stream = stream
        self.child = child
        self.fail_close = fail_close
        self.close_statuses = []

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        self.close_statuses.append(self.child.poll())
        self.stream.close()
        if self.fail_close:
            raise OSError("fixture stdin close failure")


class HeldStdinRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.children = []
        self.inputs = []
        self.launches = []
        self.original_launch = subprocess.Popen

    def launch(self, arguments, **kwargs):
        child = self.original_launch(arguments, **kwargs)
        self.children.append(child)
        self.addCleanup(self.close_child, child)
        self.launches.append((arguments, kwargs))
        if child.stdin is not None:
            child.stdin = TrackedInput(child.stdin, child, fail_close=getattr(self, "fail_close", False))
            self.inputs.append(child.stdin)
        return child

    @staticmethod
    def close_child(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(2)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                target = stream.stream if isinstance(stream, TrackedInput) else stream
                target.close()

    def run_child(self, code, **kwargs):
        with mock.patch.object(commands.subprocess, "Popen", side_effect=self.launch):
            return commands.run_command(
                [sys.executable, "-c", code], env=dict(os.environ),
                timeout=kwargs.pop("timeout", 2), log_path=self.root / "command.log", **kwargs,
            )

    def assert_held_closed_after_exit(self):
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(self.inputs), 1)
        stream = self.inputs[0]
        self.assertTrue(stream.closed)
        self.assertEqual(len(stream.close_statuses), 1)
        self.assertIsNotNone(stream.close_statuses[0], "stdin closed before owned child exited")
        self.assertIsNotNone(self.children[0].poll())

    def test_default_stays_devnull(self):
        result = self.run_child("import sys; print(len(sys.stdin.buffer.read()))")
        self.assertEqual(result.output.strip(), "0")
        self.assertEqual(self.launches[0][1]["stdin"], subprocess.DEVNULL)
        self.assertFalse(self.inputs)

    def test_held_pipe_has_no_eof_or_input_until_child_exits(self):
        result = self.run_child(
            "import select,sys; readable=select.select([sys.stdin],[],[],0.1)[0]; "
            "print('closed' if readable else 'held'); raise SystemExit(7 if readable else 0)",
            hold_stdin_open=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output.strip(), "held")
        self.assert_held_closed_after_exit()
        options = self.launches[0][1]
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual(options["bufsize"], 0)
        self.assertTrue(options["close_fds"])
        self.assertFalse(options.get("start_new_session", False))

    def test_native_nonzero_and_stdout_are_not_overridden(self):
        result = self.run_child("print('1'); raise SystemExit(255)", hold_stdin_open=True)
        self.assertEqual(result.returncode, 255)
        self.assertEqual(result.output.strip(), "1")
        self.assert_held_closed_after_exit()

    def test_invalid_mode_and_input_incompatibility_rejected_before_launch(self):
        for options in (
            {"hold_stdin_open": 1}, {"hold_stdin_open": None}, {"hold_stdin_open": "yes"},
            {"hold_stdin_open": True, "input_data": b""},
            {"hold_stdin_open": True, "input_data": b"x"},
        ):
            with self.subTest(options=options), mock.patch.object(commands.subprocess, "Popen") as launch:
                with self.assertRaises(LeaseError):
                    commands.run_command(
                        [sys.executable, "-c", "pass"], env={}, timeout=1,
                        log_path=self.root / "unused.log", **options,
                    )
                launch.assert_not_called()

    def test_existing_bounded_input_semantics_unchanged(self):
        result = self.run_child(
            "import sys; print(sys.stdin.buffer.read().decode())", input_data=b"fixture",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output.strip(), "fixture")
        self.assert_held_closed_after_exit_if_any_input()

    def assert_held_closed_after_exit_if_any_input(self):
        self.assertEqual(len(self.inputs), 1)
        self.assertTrue(self.inputs[0].closed)
        self.assertEqual(self.launches[0][1]["stdin"], subprocess.PIPE)

    def test_timeout_reaps_then_closes_held_pipe(self):
        with self.assertRaises(commands.CommandTimeout):
            self.run_child("import time; time.sleep(20)", timeout=0.1, hold_stdin_open=True)
        self.assert_held_closed_after_exit()

    def test_cancellation_reaps_then_closes_held_pipe(self):
        event = Event()
        timer = Timer(0.1, event.set)
        timer.start()
        self.addCleanup(timer.join)
        with self.assertRaises(commands.CommandCancelled):
            self.run_child("import time; time.sleep(20)", cancelled=event, hold_stdin_open=True)
        self.assert_held_closed_after_exit()

    def test_supervision_setup_failure_reaps_then_closes_held_pipe(self):
        for target in ("start", "constructor"):
            with self.subTest(target=target):
                self.children.clear()
                self.inputs.clear()
                self.launches.clear()
                patch_target = (
                    "resourcelease.process._OutputCollector.start" if target == "start"
                    else "resourcelease.process._OutputCollector"
                )
                with mock.patch(patch_target, side_effect=OSError("fixture setup")):
                    with self.assertRaises(OSError):
                        self.run_child("import time; time.sleep(20)", hold_stdin_open=True)
                self.assert_held_closed_after_exit()

    def test_output_error_still_closes_held_pipe(self):
        original_join = commands._OutputCollector.join

        def failed_output(collector, *args, **kwargs):
            original_join(collector, *args, **kwargs)
            collector.error = OSError("fixture output failure")

        with mock.patch.object(commands._OutputCollector, "join", failed_output):
            with self.assertRaises(LeaseError):
                self.run_child("print('1')", hold_stdin_open=True)
        self.assert_held_closed_after_exit()

    def test_stuck_output_pipe_keeps_quarantine_and_closes_stdin(self):
        uncertainty = QuarantineError("fixture pipe drain uncertainty")
        with mock.patch.object(commands._OutputCollector, "join", side_effect=uncertainty):
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("print('1'); raise SystemExit(255)", hold_stdin_open=True)
        self.assertIs(raised.exception, uncertainty)
        self.assert_held_closed_after_exit()

    def test_stdin_close_failure_is_quarantine_not_success(self):
        self.fail_close = True
        with self.assertRaises(QuarantineError):
            self.run_child("pass", hold_stdin_open=True)
        self.assert_held_closed_after_exit()

    def test_close_error_does_not_mask_reap_uncertainty(self):
        self.fail_close = True
        uncertainty = QuarantineError("fixture original reap uncertainty")
        original_stop = commands.ManagedProcess.stop

        def uncertain_stop(child, **kwargs):
            original_stop(child, **kwargs)
            raise uncertainty

        with mock.patch.object(commands.ManagedProcess, "stop", uncertain_stop):
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("import time; time.sleep(20)", timeout=0.1, hold_stdin_open=True)
        self.assertIs(raised.exception, uncertainty)
        self.assert_held_closed_after_exit()

    def test_close_error_does_not_mask_setup_cleanup_uncertainty(self):
        self.fail_close = True
        uncertainty = QuarantineError("fixture original setup cleanup uncertainty")
        original_stop = commands._stop_after_setup_failure

        def uncertain_stop(child):
            original_stop(child)
            raise uncertainty

        with mock.patch.object(
            commands._OutputCollector, "start", side_effect=OSError("fixture original setup"),
        ), mock.patch.object(commands, "_stop_after_setup_failure", uncertain_stop):
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("import time; time.sleep(20)", hold_stdin_open=True)
        self.assertIs(raised.exception, uncertainty)
        self.assert_held_closed_after_exit()

    def test_stop_reap_oserror_remains_quarantine_even_with_stdin_close_error(self):
        self.fail_close = True
        error = OSError("fixture stop/reap failure")
        original_stop = commands.ManagedProcess.stop

        def failed_stop(child, **kwargs):
            original_stop(child, **kwargs)
            raise error

        with mock.patch.object(commands.ManagedProcess, "stop", failed_stop):
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("import time; time.sleep(20)", timeout=0.1, hold_stdin_open=True)
        self.assertIs(raised.exception.__cause__, error)
        self.assert_held_closed_after_exit()

    def test_setup_reap_oserror_keeps_quarantine_priority(self):
        error = OSError("fixture setup reap failure")
        original_stop = commands._stop_after_setup_failure

        def failed_stop(child):
            original_stop(child)
            raise error

        with mock.patch.object(
            commands._OutputCollector, "start", side_effect=OSError("fixture setup"),
        ), mock.patch.object(commands, "_stop_after_setup_failure", failed_stop):
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("import time; time.sleep(20)", hold_stdin_open=True)
        self.assertIs(raised.exception.__cause__, error)
        self.assert_held_closed_after_exit()

    def test_unreaped_child_still_closes_stdin_after_single_failed_stop_attempt(self):
        self.fail_close = True
        uncertainty = QuarantineError("fixture owned child still running")
        with mock.patch.object(commands.ManagedProcess, "stop", side_effect=uncertainty) as stop:
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("import time; time.sleep(20)", timeout=0.1, hold_stdin_open=True)
        self.assertIs(raised.exception, uncertainty)
        stop.assert_called_once()
        self.assertIsNone(self.children[0].poll())
        self.assertTrue(self.inputs[0].closed)
        self.assertEqual(self.inputs[0].close_statuses, [None])
        # The test owns the original live handle and registered its cleanup at
        # launch. No PID lookup/adoption is used to settle this negative fixture.

    def test_setup_unreaped_child_closes_stdin_without_masking_uncertainty(self):
        uncertainty = QuarantineError("fixture setup child still running")
        with mock.patch.object(
            commands._OutputCollector, "start", side_effect=OSError("fixture setup"),
        ), mock.patch.object(
            commands, "_stop_after_setup_failure", side_effect=uncertainty,
        ) as stop:
            with self.assertRaises(QuarantineError) as raised:
                self.run_child("import time; time.sleep(20)", hold_stdin_open=True)
        self.assertIs(raised.exception, uncertainty)
        stop.assert_called_once()
        self.assertIsNone(self.children[0].poll())
        self.assertTrue(self.inputs[0].closed)
        self.assertEqual(self.inputs[0].close_statuses, [None])

    def test_partially_started_collector_is_joined_after_setup_failure(self):
        collectors = []
        original_start = commands._OutputCollector.start

        def failed_start(collector):
            original_start(collector)
            collectors.append(collector)
            raise OSError("fixture after collector thread start")

        with mock.patch.object(commands._OutputCollector, "start", failed_start):
            with self.assertRaises(OSError):
                self.run_child("import time; time.sleep(20)", hold_stdin_open=True)
        self.assert_held_closed_after_exit()
        self.assertFalse(collectors[0]._thread.is_alive())
