import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adhd import core, leases, snapshot_reader as reader


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root / 'repo'
        self.ws.mkdir()
        self.scratch = self.root / 'scratch'
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Snapshot Test')
        self.git('config', 'core.autocrlf', 'false')
        (self.ws / 'main.py').write_bytes(b'value = 1\nprint(value)\n')
        (self.ws / '.env').write_text('sensitive original', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.ws), *args], check=True,
                              capture_output=True, timeout=20).stdout

    def test_fixed_commit_excludes_credentials_and_detects_tampering(self):
        result = reader.prepare(self.ws, scratch=self.scratch)
        self.assertTrue(result['point_in_time_consistent'])
        self.assertFalse(result['concurrent_reader']['enabled'])
        self.assertEqual(result['excluded'][0]['reason'], 'credential_path')
        ref = Path(result['snapshot'])
        actual = reader.read(ref, result['manifest_hash'], 'main.py', end_line=1)
        self.assertEqual(actual['text'], 'value = 1\n')
        self.assertEqual(actual['ref']['start_line'], 1)
        copy = Path(result['content']) / 'worktree' / 'main.py'
        copy.write_text('tampered', encoding='utf-8')
        self.assertEqual(reader.read(ref, result['manifest_hash'], 'main.py')['status'], 'stale')
        self.assertIn('value = 1', (self.ws / 'main.py').read_text())

    def test_bounded_fragments_retrieve_the_rest_of_a_long_line(self):
        result=reader.prepare(self.ws,scratch=self.scratch)
        arguments={'start_line':1}
        pieces=[]
        for _ in range(30):
            packet=reader.read(Path(result['snapshot']),result['manifest_hash'],'main.py',
                               max_chars=3,**arguments)
            self.assertLessEqual(len(packet['text']),3)
            pieces.append(packet['text'])
            if not packet['continuation']:
                break
            arguments=packet['continuation']
        self.assertEqual(''.join(pieces),(self.ws/'main.py').read_text())
        exact=reader.read(Path(result['snapshot']),result['manifest_hash'],'main.py',
                          max_chars=len(''.join(pieces)))
        self.assertFalse(exact['truncated'])

    def test_dirty_keeps_index_and_worktree_and_only_explicit_untracked(self):
        source = self.ws / 'main.py'
        source.write_text('value = 2\n', encoding='utf-8')
        self.git('add', 'main.py')
        source.write_text('value = 3\n', encoding='utf-8')
        (self.ws / 'allowed.txt').write_text('allowed')
        (self.ws / 'other.txt').write_text('not requested')
        result = reader.prepare(self.ws, dirty=True, untracked=['allowed.txt'], scratch=self.scratch)
        content = Path(result['content'])
        self.assertEqual((content / 'index/main.py').read_text(), 'value = 2\n')
        self.assertEqual((content / 'worktree/main.py').read_text(), 'value = 3\n')
        self.assertNotIn('other.txt', result['files'])
        self.assertTrue(result['writer_checkpoint_required'])
        fixed = reader.prepare(self.ws, dirty=True, require_point_in_time=True, scratch=self.scratch)
        self.assertTrue(fixed['fixed_commit_fallback'])
        self.assertFalse(fixed['dirty'])

    def test_no_scratch_escape_or_concurrent_write_promise(self):
        with self.assertRaises(ValueError):
            reader.prepare(self.ws, scratch=self.ws / 'scratch')
        with self.assertRaises(ValueError):
            reader.prepare(self.ws, dirty=True, untracked=['../outside'], scratch=self.scratch)
        try:
            (self.ws / 'escape.txt').symlink_to(self.root / 'outside')
        except OSError:
            return  # Windows may deny symlink creation; path traversal is still exercised.
        with self.assertRaises(ValueError):
            reader.prepare(self.ws, dirty=True, untracked=['escape.txt'], scratch=self.scratch)

    def test_writer_conflict_staleness_generation_and_child_drain(self):
        snap = reader.prepare(self.ws, scratch=self.scratch)
        ref, sha = Path(snap['snapshot']), snap['manifest_hash']
        first = reader.claim_writer(ref, sha, run_id='legacy-owner', owner='legacy')
        self.assertEqual(first['status'], 'writer_acquired')
        conflict = reader.claim_writer(ref, sha, run_id='other-owner', owner='legacy')
        self.assertEqual(conflict['status'], 'conflict')
        self.assertFalse(leases.release(self.ws, 'legacy-owner', first['generation'] + 1))
        self.assertTrue(leases.release(self.ws, 'legacy-owner', first['generation']))
        (self.ws / 'main.py').write_text('value = 99\n')
        key = 'a' * 24
        run_id = 'native:' + key + ':run'
        state = core.store() / 'native' / key / 'state.json'
        core.atomic_json(state, {'children': {'child': {'status': 'running'}}})
        stale = reader.claim_writer(ref, sha, run_id=run_id, owner='native')
        self.assertEqual(stale['status'], 'stale')
        self.assertTrue(stale['children_running'])
        self.assertTrue(stale['lease_acquired'])
        self.assertTrue(leases.release(self.ws, run_id, stale['generation']))

    def test_changed_during_copy_retries_then_uses_fixed_commit(self):
        original = reader._source
        def moving(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2]:
                source = self.ws / 'main.py'
                source.write_text(source.read_text() + '# changed\n')
            return result
        with patch.object(reader, '_source', side_effect=moving):
            result = reader.prepare(self.ws, dirty=True, scratch=self.scratch)
        self.assertTrue(result['fixed_commit_fallback'])
        self.assertEqual(result['dirty_attempts'], 3)
        self.assertTrue(result['point_in_time_consistent'])
        self.assertEqual(list(self.scratch.iterdir()),[Path(result['snapshot']).parent])

    def test_index_change_and_probe_failure_cannot_silently_keep_lease(self):
        snap=reader.prepare(self.ws,scratch=self.scratch)
        source=self.ws/'main.py'
        initial=source.read_bytes()
        source.write_bytes(b'value = 4\n')
        self.git('add','main.py')
        source.write_bytes(initial)
        result=reader.claim_writer(Path(snap['snapshot']),snap['manifest_hash'],run_id='index-test',owner='legacy')
        self.assertEqual(result['status'],'stale')
        self.assertFalse(result['lease_acquired'])
        with patch.object(reader,'_source',side_effect=OSError('unavailable')):
            failed=reader.claim_writer(Path(snap['snapshot']),snap['manifest_hash'],run_id='probe-test',owner='legacy')
        self.assertEqual(failed['status'],'blocked')
        self.assertFalse(failed['lease_acquired'])


if __name__ == '__main__':
    unittest.main()
