from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from adhd import dependencies as _dependencies
from adhd import runner


@unittest.skipUnless(os.name == 'nt' or sys.platform.startswith('linux'),
                     'descendant ownership is implemented on Windows and Linux')
class RunnerProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _command(self, folder, *, parent_sleep=0, exit_code=0):
        folder.mkdir()
        child = (
            'import pathlib,sys,time; '
            'pathlib.Path(sys.argv[1]).write_text("ready"); '
            'time.sleep(.8); pathlib.Path(sys.argv[2]).write_text("late")'
        )
        parent = (
            'import json,os,pathlib,subprocess,sys,time; '
            'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]); '
            'print(json.dumps({"owner":os.environ.get("ADHD_EXEC_OWNER"),'
            '"cwd":os.getcwd(),"input":sys.stdin.buffer.read().decode("utf-8")}),flush=True); '
            'print("stderr preserved",file=sys.stderr,flush=True); '
            'until=time.monotonic()+5\n'
            'while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<until: time.sleep(.01)\n'
            'assert pathlib.Path(sys.argv[2]).exists(), "descendant never started"\n'
            'time.sleep(float(sys.argv[4])); sys.exit(int(sys.argv[5]))'
        )
        return [sys.executable, '-c', parent, child, str(folder / 'ready.txt'),
                str(folder / 'late.txt'), str(parent_sleep), str(exit_code)]

    def _wait_ready(self, folder):
        until = time.monotonic() + 5
        while not (folder / 'ready.txt').exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue((folder / 'ready.txt').exists(), 'descendant never started')

    def _assert_no_late_write(self, folders):
        # The child would write after .8 seconds if the runner left it alive.
        time.sleep(1)
        for folder in folders:
            with self.subTest(folder=folder.name):
                self.assertFalse((folder / 'late.txt').exists())

    def test_completed_commands_stop_descendants_and_preserve_output(self):
        folders = []
        for execute, owner, exit_code in (
            (runner.execute, 'legacy', 0),
            (runner.execute_native_check, 'native-check', 7),
        ):
            with self.subTest(owner=owner):
                folder = self.root / owner
                # The command waits for its child to start before exiting.
                # The runner must still own it once the root exit is available.
                argv = self._command(folder, exit_code=exit_code)
                prefix = folder / 'check'
                input_text = '검증 input' if owner == 'legacy' else ''
                options = {'input_text': input_text} if owner == 'legacy' else {}
                code = execute(argv, folder, prefix, timeout=5, **options)
                self.assertEqual(code, exit_code)
                receipt = json.loads(prefix.with_suffix('.stdout.jsonl').read_text(encoding='utf-8'))
                self.assertEqual(receipt, {
                    'owner': owner, 'cwd': str(folder.resolve()), 'input': input_text,
                })
                self.assertEqual(prefix.with_suffix('.stderr.log').read_text().splitlines(),
                                 ['stderr preserved'])
                self.assertEqual(prefix.with_suffix('.input.txt').read_text(encoding='utf-8'),
                                 input_text)
                folders.append(folder)
        self._assert_no_late_write(folders)

    def test_timeout_stops_started_descendants(self):
        folder = self.root / 'timeout'
        argv = self._command(folder, parent_sleep=10)
        started = time.monotonic()

        def deadline_after_spawn():
            # Start the timeout only once a real child exists, avoiding a race
            # with Python/Windows launcher startup on slower CI machines.
            if (folder / 'ready.txt').exists():
                return started + 2
            if time.monotonic() - started > 5:
                self.fail('descendant never started')
            return started

        clock = SimpleNamespace(monotonic=deadline_after_spawn, sleep=time.sleep)
        with patch.object(runner, 'time', clock):
            with self.assertRaises(runner.Halt) as raised:
                runner.execute(argv, folder, folder / 'check', timeout=1)
        self.assertEqual(raised.exception.status, 'budget_exhausted')
        self.assertTrue((folder / 'ready.txt').exists())
        self._assert_no_late_write([folder])

    def test_cancellation_stops_started_descendants(self):
        folder = self.root / 'cancel'
        argv = self._command(folder, parent_sleep=10)
        with self.assertRaises(runner.Halt) as raised:
            runner.execute(argv, folder, folder / 'check', timeout=5,
                           stop_file=folder / 'ready.txt')
        self.assertEqual(raised.exception.status, 'cancelled')
        self.assertTrue((folder / 'ready.txt').exists())
        self._assert_no_late_write([folder])

    def test_unexpected_error_stops_started_descendants(self):
        folder = self.root / 'error'
        argv = self._command(folder, parent_sleep=10)
        wait_ready = self._wait_ready

        class UnreadableStopFile:
            def exists(self):
                wait_ready(folder)
                raise OSError('stop marker cannot be read')

        with self.assertRaisesRegex(OSError, 'stop marker cannot be read'):
            runner.execute_native_check(argv, folder, folder / 'check', timeout=5,
                                        stop_file=UnreadableStopFile())
        self._assert_no_late_write([folder])


class PosixFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_macos_keeps_existing_launch_and_exit_code(self):
        process = Mock()
        process.poll.return_value = 7
        process.returncode = 7
        with patch.object(runner, 'sys', SimpleNamespace(platform='darwin')), \
                patch.object(runner, 'os', SimpleNamespace(name='posix', environ={})), \
                patch.object(runner, 'OwnedProcess') as owned, \
                patch.object(runner.subprocess, 'Popen', return_value=process) as launch:
            code = runner.execute(['check'], self.root, self.root / 'check')
        self.assertEqual(code, 7)
        owned.assert_not_called()
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(launch.call_args.kwargs['env']['ADHD_EXEC_OWNER'], 'legacy')
        process.close.assert_not_called()

    def test_macos_still_terminates_on_cancellation(self):
        process = Mock()
        process.poll.return_value = None
        stop = self.root / 'STOP'
        stop.write_text('stop')
        with patch.object(runner, 'sys', SimpleNamespace(platform='darwin')), \
                patch.object(runner, 'os', SimpleNamespace(name='posix', environ={})), \
                patch.object(runner, 'OwnedProcess') as owned, \
                patch.object(runner.subprocess, 'Popen', return_value=process), \
                patch.object(runner, 'terminate_tree') as terminate:
            with self.assertRaises(runner.Halt) as raised:
                runner.execute_native_check(['check'], self.root, self.root / 'check', stop_file=stop)
        self.assertEqual(raised.exception.status, 'cancelled')
        owned.assert_not_called()
        terminate.assert_called_once_with(process)
        process.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
