import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adhd import context_session, native, immutable_gc
from tests import test_native as native_fixture


class StatusRefreshTests(unittest.TestCase):
    def test_oversized_untrusted_status_metadata_never_writes_unbounded_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, key = Path(temporary), 'b' * 24
            with self.assertRaises(ValueError):
                context_session.refresh_status({'workspace': str(root), 'key': key,
                    'status': 'untrusted' * 20000, 'contract': {'criteria': []}})
            self.assertFalse((root / '.adhd/context-status.json').exists())

    def test_original_crlf_goal_has_stable_markdown_without_repeated_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, key = Path(temporary), 'b' * 24
            view = {'workspace': str(root), 'key': key, 'run_id': 'c' * 32, 'mode': 'coding',
                    'status': 'working', 'contract': {'criteria': []},
                    'goal_request': {'outcome': '원래 요청\r\n다음 줄'}}
            context_session.refresh_status(view)
            markdown = root / '.adhd/context-status.md'
            modified = markdown.stat().st_mtime_ns
            result = json.loads((root / '.adhd/context-status.json').read_text(encoding='utf-8'))
            self.assertEqual('원래 요청\r\n다음 줄', result['goal'])
            self.assertIn('원래 요청\n다음 줄', result['markdown'])
            self.assertEqual(result['markdown'], markdown.read_text(encoding='utf-8'))
            context_session.refresh_status(view)
            self.assertEqual(modified, markdown.stat().st_mtime_ns)

    def test_nongit_native_contract_does_not_spawn_automatic_git_probes(self):
        fixture = native_fixture.NativeTests()
        fixture.setUp()
        try:
            with patch.object(context_session, '_changes', side_effect=AssertionError('unexpected Git probe')):
                fixture.begin()
            view = json.loads((fixture.ws / '.adhd/bridge' / fixture.key / 'view.json').read_text())
            self.assertEqual('available', view['status_panel']['status'])
            result = json.loads((fixture.ws / '.adhd/context-status.json').read_text(encoding='utf-8'))
            self.assertFalse(result['changes']['complete'])
            self.assertEqual('native_git_scope_not_established', result['changes']['reason'])
        finally:
            fixture.tearDown()

    def test_gc_accepts_only_bound_derived_status_and_manual_snapshot(self):
        fixture = native_fixture.NativeTests()
        fixture.setUp()
        try:
            fixture.begin()
            self.assertTrue(fixture.candidate()['ok'])
            fixture.spawn()
            fixture.review()
            state = fixture.state()
            self.assertEqual('complete', state['status'])
            expected = {'context-status.json', 'context-status.md'}
            def accepted():
                return immutable_gc._derived_status_sidecars(fixture.ws, fixture.key, state['run_id'], state)
            self.assertEqual(expected, accepted())
            context_session.status(fixture.ws, fixture.key, write=True)
            self.assertEqual(expected, accepted())
            path = fixture.ws / '.adhd/context-status.json'
            original = path.read_bytes()
            for changes in ({'schema_version': True}, {'session': 'f' * 24}, {'unknown_refs': ['unregistered-root']},
                            {'authority': 'acceptance'}, {'run_id': 'e' * 32}):
                value = json.loads(original)
                value.update(changes)
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    accepted()
            path.write_bytes(original)
            markdown = fixture.ws / '.adhd/context-status.md'
            markdown.write_text('unbound replacement', encoding='utf-8')
            with self.assertRaises(ValueError):
                accepted()
        finally:
            fixture.tearDown()

    def test_native_state_transition_refreshes_panel_files(self):
        fixture = native_fixture.NativeTests()
        fixture.setUp()
        try:
            fixture.begin()
            target = fixture.ws / '.adhd/context-status.json'
            self.assertTrue(target.is_file())
            before = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual('working', before['phase'])
            self.assertEqual('derived_not_acceptance', before['authority'])
            ack = fixture.request('checkpoint', {'summary': 'Implemented status refresh',
                                                'next_action': 'Run the focused regression'})
            self.assertTrue(ack['ok'], ack)
            after = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual('Run the focused regression', after['next_action'])
            self.assertNotEqual(before['projection_sha256'], after['projection_sha256'])
            self.assertIn(after['next_action'],
                          target.with_suffix('.md').read_text(encoding='utf-8'))
            view = json.loads((fixture.ws / '.adhd/bridge' / fixture.key / 'view.json').read_text())
            self.assertEqual('.adhd/context-status.md', view['status_panel']['file'])
            self.assertEqual('unstaged', view['status_panel']['review']['view'])
        finally:
            fixture.tearDown()

    def test_unchanged_state_does_not_rescan_git_or_rewrite_status(self):
        fixture = native_fixture.NativeTests()
        fixture.setUp()
        try:
            fixture.begin()
            target = fixture.ws / '.adhd/context-status.md'
            self.assertTrue(target.is_file())
            before = (target.stat().st_mtime_ns, target.read_bytes())
            with patch.object(context_session, '_changes') as changes:
                native.export_view(fixture.state())
            changes.assert_not_called()
            self.assertEqual(before, (target.stat().st_mtime_ns, target.read_bytes()))
        finally:
            fixture.tearDown()

    def test_panel_failure_does_not_change_native_authority(self):
        fixture = native_fixture.NativeTests()
        fixture.setUp()
        try:
            fixture.begin()
            with patch.object(context_session, 'refresh_status', side_effect=PermissionError('denied')):
                ack = fixture.request('checkpoint', {'summary': 'Panel access is unavailable',
                                                    'next_action': 'Preserve native state'})
            self.assertTrue(ack['ok'], ack)
            state = fixture.state()
            self.assertEqual('working', state['status'])
            view = json.loads((fixture.ws / '.adhd/bridge' / fixture.key / 'view.json').read_text())
            self.assertEqual('unavailable', view['status_panel']['status'])
            self.assertEqual('PermissionError', view['status_panel']['error'])
            self.assertNotIn('denied', json.dumps(view['status_panel']))
        finally:
            fixture.tearDown()

    def test_large_status_is_bounded_and_retains_original_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, key = Path(temporary), 'b' * 24
            view = {'workspace': str(root), 'key': key, 'run_id': 'c' * 32,
                    'status': 'working', 'contract': {'criteria': []},
                    'goal_request': {'outcome': '한국어' * 100000},
                    'test_outcomes': {str(i): {'detail': 'x' * 1000} for i in range(500)}}
            context_session.refresh_status(view)
            target = root / '.adhd/context-status.json'
            self.assertLessEqual(target.stat().st_size, 65536)
            result = json.loads(target.read_text(encoding='utf-8'))
            self.assertIn('goal', result['truncated_fields'])
            self.assertEqual('.adhd/bridge/' + key + '/view.json', result['source']['path'])
            self.assertIn('verification', result['truncated_fields'])

    def test_deleted_panel_is_rebuilt_and_text_is_not_json_escaped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, key = Path(temporary), 'b' * 24
            goal = 'A\nB "quoted" \\path 한국어 ' * 400
            view = {'workspace': str(root), 'key': key, 'run_id': 'c' * 32,
                    'status': 'working', 'contract': {'criteria': []},
                    'goal_request': {'outcome': goal}}
            context_session.refresh_status(view)
            markdown = root / '.adhd/context-status.md'
            markdown.unlink()
            context_session.refresh_status(view)
            self.assertTrue(markdown.is_file())
            result = json.loads((root / '.adhd/context-status.json').read_text(encoding='utf-8'))
            self.assertTrue(result['goal'].startswith('A\nB "quoted" \\path 한국어'))
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8')), 65536)

    def test_derived_export_does_not_follow_a_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, key = Path(temporary), 'b' * 24
            outside = root / 'protected.txt'
            outside.write_text('preserve', encoding='utf-8')
            (root / '.adhd').mkdir()
            try:
                (root / '.adhd/context-status.md').symlink_to(outside)
            except OSError:
                self.skipTest('Symlink creation is unavailable on this host')
            with self.assertRaises(ValueError):
                context_session.refresh_status({'workspace': str(root), 'key': key,
                                                'status': 'working', 'contract': {}})
            self.assertEqual('preserve', outside.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
