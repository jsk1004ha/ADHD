from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from adhd.owned_process import OwnedProcess


class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _spawning_command(self, *, parent_sleep=0, exit_code=0):
        child = ('import pathlib,sys,time; time.sleep(.7); '
                 'pathlib.Path(sys.argv[1]).write_text("late")')
        parent = ('import os,pathlib,subprocess,sys,time; '
                  'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]); '
                  'pathlib.Path(sys.argv[5]).write_text(str(os.getpid())); '
                  'time.sleep(float(sys.argv[3])); '
                  'sys.stdout.write("root-ready\\n"); sys.exit(int(sys.argv[4]))')
        return [sys.executable, '-c', parent, child, str(self.root / 'late.txt'),
                str(parent_sleep), str(exit_code), str(self.root / 'spawned.txt')]

    def _assert_spawned(self, process):
        marker = self.root / 'spawned.txt'
        until = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue(marker.exists(), 'root never spawned its child')
        self.assertEqual(process.pid, int(marker.read_text()))

    def test_exited_parent_cannot_leave_writer_after_wait(self):
        with OwnedProcess(self._spawning_command(exit_code=7), cwd=self.root,
                          stdout=subprocess.PIPE) as process:
            self.assertEqual(process.wait(timeout=5), 7)
            self._assert_spawned(process)
            self.assertEqual(process.poll(), 7)
            self.assertIsNotNone(process.pid)
            output = process.stdout
            self.assertEqual(output.read().splitlines(), [b'root-ready'])
        self.assertTrue(output.closed)
        self.assertFalse((self.root / 'late.txt').exists())
        time.sleep(.9)
        self.assertFalse((self.root / 'late.txt').exists())
        self.assertTrue(process._closed)
        if sys.platform == 'win32':
            self.assertIsNone(process._job)
            self.assertFalse(Path(process._temp.name).exists())

    def test_timeout_then_terminate_confirms_tree_stopped(self):
        with OwnedProcess(self._spawning_command(parent_sleep=3), cwd=self.root) as process:
            self._assert_spawned(process)
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=.1)
            process.terminate()
            self.assertIsNotNone(process.wait(timeout=5))
        time.sleep(.9)
        self.assertFalse((self.root / 'late.txt').exists())

    def test_close_kills_unfinished_tree_and_closes_streams(self):
        process = OwnedProcess(self._spawning_command(parent_sleep=3), cwd=self.root,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._assert_spawned(process)
        streams = (process.stdout, process.stderr)
        process.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(all(stream.closed for stream in streams))
        time.sleep(.9)
        self.assertFalse((self.root / 'late.txt').exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows launcher-specific handles')
    def test_failed_launch_releases_all_launcher_pipes(self):
        original = subprocess.Popen
        started = []

        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            started.append(process)
            return process

        with patch('adhd.owned_process.subprocess.Popen', side_effect=capture):
            with self.assertRaises(RuntimeError):
                OwnedProcess([str(self.root / 'missing-tool')], cwd=self.root,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())
        self.assertTrue(all(stream.closed for stream in
                            (started[0].stdin, started[0].stdout, started[0].stderr)))


if __name__ == '__main__':
    unittest.main()
