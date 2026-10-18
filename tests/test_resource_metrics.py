import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from adhd.resource_metrics import ProcessTreeMonitor, disk_snapshot


class ResourceTests(unittest.TestCase):
    def test_post_task_identity_output_is_bounded_without_losing_total_or_observer(self):
        observer = os.getpid()
        pids = set(range(1000, 2000)) | {observer}
        class Backend:
            def processes(self):
                return {pid: (0, None) for pid in pids}
            def sample(self, pid):
                return {'identity': pid, 'cpu_seconds': 0.0}
        monitor = ProcessTreeMonitor(1000)
        monitor.backend = Backend()
        monitor.identities = {pid: pid for pid in pids}
        with patch('adhd.resource_metrics.time.sleep'):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertEqual(len(pids), result['remaining_count'])
        self.assertEqual(len(pids), result['remaining_identity_count'])
        self.assertEqual(32, len(result['remaining_identities']))
        self.assertTrue(result['remaining_identities_truncated'])
        self.assertTrue(result['observer_in_remaining'])
        self.assertEqual(observer, result['observer_pid'])

    def test_pid_reuse_excludes_unrelated_processes_and_children(self):
        class Backend:
            generation=0
            def processes(self):
                return {10:(0,''),20:(10,''),30:(20,'')} if self.generation==2 else {10:(0,''),20:(10,'')}
            def sample(self,pid):
                changed=(pid==20 and self.generation>=1) or (pid==10 and self.generation==2)
                return dict(identity=pid+(100 if changed else 0),cpu_seconds=self.generation+1,
                            private_bytes=1000 if changed else 1,working_set_bytes=1000 if changed else 1,
                            read_bytes=0,write_bytes=0)
        monitor=ProcessTreeMonitor(10)
        backend=Backend()
        monitor.backend=backend
        for i in range(3):
            backend.generation=i
            monitor.sample()
        result=monitor.report()
        self.assertEqual(result['observed_processes'],2)
        self.assertEqual(result['peak_private_bytes'],2)
        self.assertIn('process-identity-changed',result['errors'])
        self.assertNotIn(30,monitor.identities)
    def test_real_sampler_stops_and_reports_unknown_honestly(self):
        with ProcessTreeMonitor(os.getpid(), interval=.05) as monitor:
            until = time.monotonic() + .07
            while time.monotonic() < until:
                sum(range(2000))
        result = monitor.report()
        self.assertTrue(result['sampler_stopped'])
        self.assertGreaterEqual(result['sample_count'], 2)
        self.assertIn(result['coverage'], {'partial_sampled', 'unknown'})
        if result['observed_processes']:
            self.assertGreaterEqual(result['cpu_seconds'], 0)
            self.assertGreater(result['peak_working_set_bytes'], 0)

    def test_disk_sizes_do_not_fabricate_reclaimed_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            path.write_bytes(b'payload')
            result = disk_snapshot([path, path.with_name('missing')])
            self.assertEqual(7, result['logical_bytes'])
            self.assertEqual(1, result['missing'])
            self.assertIsNone(result['reclaimed_bytes'])

    def test_post_task_observation_distinguishes_busy_missing_and_reused(self):
        class Backend:
            phase = 0
            def processes(self):
                return {10: (0, None), 20: (10, None), 30: (10, None),
                        40: (10, None), 50: (10, None)}
            def sample(self, pid):
                if pid == 40:
                    raise OSError('inaccessible')
                identity = pid + (1000 if pid == 50 and self.phase else 0)
                return dict(identity=identity, cpu_seconds=(1.0 + self.phase if pid == 20 else 1.0),
                            private_bytes=1, working_set_bytes=1, read_bytes=0, write_bytes=0)
        monitor = ProcessTreeMonitor(10)
        backend = Backend()
        monitor.backend = backend
        monitor.identities = {10: 10, 20: 20, 30: 30, 40: 40, 50: 50}
        def advance(_):
            backend.phase = 1
        with patch('adhd.resource_metrics.time.sleep', side_effect=advance):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertEqual(result['remaining_count'], 3)
        self.assertEqual(result['remaining_cpu_delta_seconds'], 1.0)
        self.assertEqual(result['unavailable_pids'], [40])
        self.assertEqual(result['reused_pids'], [50])
        self.assertIsNone(result['remaining_idle_cpu_observed'])
        self.assertTrue(result['sampler_stopped'])

    def test_post_task_observation_empty_tree_has_no_residual_cpu(self):
        monitor = ProcessTreeMonitor(10)
        monitor.identities = {10: 10}
        class Backend:
            def processes(self):
                return {}
        monitor.backend = Backend()
        with patch('adhd.resource_metrics.time.sleep'):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertEqual(result['remaining_identities'], [])
        self.assertEqual(result['remaining_count'], 0)
        self.assertEqual(result['remaining_cpu_delta_seconds'], 0.0)
        self.assertIsNone(result['remaining_idle_cpu_observed'])

    def test_post_task_idle_is_limited_to_a_confirmed_tracked_identity(self):
        monitor = ProcessTreeMonitor(10)
        monitor.identities = {10: 99}
        class Backend:
            def processes(self):
                return {10: (0, None)}
            def sample(self, pid):
                return dict(identity=99, cpu_seconds=2.0, private_bytes=None,
                            working_set_bytes=None, read_bytes=None, write_bytes=None)
        monitor.backend = Backend()
        with patch('adhd.resource_metrics.time.sleep'):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertEqual(result['remaining_cpu_delta_seconds'], 0.0)
        self.assertTrue(result['remaining_idle_cpu_observed'])
        self.assertEqual(result['cpu_coverage'], 'complete_tracked')
        self.assertIn('tracked PID', result['scope'])

    def test_post_task_unknown_when_no_identity_was_tracked(self):
        monitor = ProcessTreeMonitor(10)
        monitor.backend = type('Backend', (), {'processes': lambda self: {}})()
        with patch('adhd.resource_metrics.time.sleep'):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertIsNone(result['remaining_cpu_delta_seconds'])
        self.assertIsNone(result['remaining_count'])
        self.assertEqual(result['cpu_coverage'], 'partial')

    def test_post_task_unavailable_snapshot_does_not_report_zero(self):
        monitor = ProcessTreeMonitor(10)
        monitor.identities = {10: 99}
        class Backend:
            def processes(self):
                raise OSError('snapshot unavailable')
        monitor.backend = Backend()
        with patch('adhd.resource_metrics.time.sleep'):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertIsNone(result['remaining_count'])
        self.assertIsNone(result['remaining_cpu_delta_seconds'])
        self.assertEqual(result['unavailable_pids'], [10])

    def test_post_task_reused_pid_is_excluded_without_zero_cpu_claim(self):
        monitor = ProcessTreeMonitor(10)
        monitor.identities = {10: 99}
        class Backend:
            def processes(self):
                return {10: (0, None)}
            def sample(self, pid):
                return dict(identity=100, cpu_seconds=9.0)
        monitor.backend = Backend()
        with patch('adhd.resource_metrics.time.sleep'):
            result = monitor.observe_post_task(window_seconds=.05)
        self.assertEqual(result['reused_pids'], [10])
        self.assertIsNone(result['remaining_count'])
        self.assertIsNone(result['remaining_cpu_delta_seconds'])

    def test_disk_free_space_is_measured_on_each_input_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths=[Path(temporary)/name/'data' for name in ('one','two')]
            for path in paths:
                path.parent.mkdir();path.write_bytes(b'payload')
            actual_stat=Path.stat
            def stat(path,*args,**kwargs):
                info=actual_stat(path,*args,**kwargs)
                if path in paths:
                    fields=list(info);fields[2]=100+paths.index(path)
                    return os.stat_result(fields)
                return info
            def usage(path):
                self.assertIn(path,[p.parent for p in paths])
                return type('Usage',(),{'free':1000 if path==paths[0].parent else 2000})()
            with patch.object(Path,'stat',stat),patch('adhd.resource_metrics.shutil.disk_usage',usage):
                result=disk_snapshot(paths)
            self.assertIsNone(result['volume_free_bytes'])
            self.assertEqual(sorted(v['free_bytes'] for v in result['volumes']),[1000,2000])
