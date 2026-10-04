from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from adhd.core import digest
from adhd.large_tasks import LargeTaskStore
from adhd.validation_batch import run_batch


def git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    proc = subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'user.name=Fixture',
                           '-c', 'user.email=fixture@example.invalid', '-C', str(cwd), *args],
                          capture_output=True, check=True, env=env)
    return proc.stdout.decode().strip()


class LargeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.run_id = 'fixture-' + uuid.uuid4().hex[:12]

    def repository(self):
        git(self.root, 'init', '-q')
        (self.root / '.gitignore').write_text('.adhd/\n', encoding='utf-8')
        (self.root / 'outside.txt').write_text('user data\n', encoding='utf-8')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-qm', 'base')

    def spec(self, *, tasks=None, revision=1, requirement_ids=('R1', 'R2')):
        original = 'Build A and B. Preserve the original request.\n'
        requirements = [{'id': ident, 'text': 'Make ' + ident,
                         'source_turn_id': 'U1', 'source_excerpt': 'Build A and B'}
                        for ident in requirement_ids]
        tasks = tasks or [
            {'id': 'A', 'package': 'a', 'objective': 'Produce A',
             'requirements': ['R1'], 'write_paths': ['a.txt']},
            {'id': 'B', 'package': 'b', 'objective': 'Produce B',
             'requirements': ['R2'], 'write_paths': ['b.txt']}]
        return {'run_id': self.run_id, 'contract_revision': revision,
                'contract_hash': digest({'revision': revision, 'tasks': tasks}),
                'original_turns': [{'id': 'U1', 'text': original}],
                'requirements': requirements, 'tasks': tasks}

    def store(self, spec=None):
        store = LargeTaskStore(self.root, self.run_id)
        store.initialize(spec or self.spec())
        return store

    def limits(self, slots=6):
        return {'host_slots': slots, 'policy_slots': slots, 'resource_slots': slots,
                'budget_slots': slots, 'integration_backlog_limit': slots}

    def test_final_acceptance_dependency_is_rejected_before_dispatch(self):
        spec = self.spec()
        spec['tasks'][1]['depends_on'] = [{'task_id': 'A', 'needs': 'accepted'}]
        with self.assertRaisesRegex(ValueError, 'final assembly acceptance'):
            self.store(spec)

    def test_real_parallel_worktrees_stage_one_snapshot_and_preserve_checkout(self):
        self.repository()
        store = self.store()
        cards = store.dispatch('director', self.limits())
        self.assertEqual({card['id'] for card in cards}, {'A', 'B'})
        self.assertNotEqual(cards[0]['workspace'], cards[1]['workspace'])
        for card in cards:
            worker = Path(card['workspace'])
            (worker / card['id'].lower()).with_suffix('.txt').write_text(card['id'] + '\n', encoding='utf-8')
            git(worker, 'add', '.')
            git(worker, 'commit', '-qm', 'worker ' + card['id'])
            store.submit(card['id'], card['generation'], git(worker, 'rev-parse', 'HEAD'), owner=card['owner'])
        store.stage('B')
        store.stage('A')
        frozen = store.freeze()
        self.assertEqual(frozen['files']['a.txt'], frozen['files']['a.txt'])
        self.assertEqual((self.root / 'outside.txt').read_text(), 'user data\n')
        self.assertFalse((self.root / 'a.txt').exists())
        self.assertEqual(store.assert_snapshot(frozen['snapshot_digest']), frozen)
        (Path(frozen['staging_workspace']) / 'a.txt').write_text('mutated after freeze', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Frozen .*changed'):
            store.assert_snapshot(frozen['snapshot_digest'])
        (Path(frozen['staging_workspace']) / 'a.txt').write_text('A\n', encoding='utf-8')
        (Path(frozen['staging_workspace']) / 'unexpected.txt').write_text('new source', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'visible file inventory changed'):
            store.assert_snapshot_no_git(frozen['snapshot_digest'])
        (Path(frozen['staging_workspace']) / 'unexpected.txt').unlink()
        index = Path(frozen['git_metadata']['index']['path'])
        with index.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'Git index changed'):
            store.assert_snapshot_no_git(frozen['snapshot_digest'])

    def test_batch_receipts_bind_full_frozen_assembly_and_requirement_coverage(self):
        self.repository()
        store = self.store()
        cards = store.dispatch('director', self.limits())
        for card in cards:
            worker = Path(card['workspace'])
            filename = card['id'].lower() + '.txt'
            (worker / filename).write_text(card['id'] + '\n', encoding='utf-8')
            git(worker, 'add', filename)
            git(worker, 'commit', '-qm', 'worker')
            store.submit(card['id'], card['generation'], git(worker, 'rev-parse', 'HEAD'))
            store.stage(card['id'])
        frozen = store.freeze()
        stage = Path(frozen['staging_workspace'])
        plan = {'run_id': self.run_id, 'contract_revision': 1,
                'contract_hash': frozen['contract_hash'],
                'assembly_digest': frozen['snapshot_digest'],
                'snapshot_paths': list(frozen['files']), 'requirements': ['R1', 'R2'],
                'mandatory_checks': ['integration'], 'process_slots': 1,
                'checks': [{'id': 'integration', 'argv': [sys.executable, '-c',
                            'from pathlib import Path; assert Path("a.txt").read_text()=="A\\n"; assert Path("b.txt").read_text()=="B\\n"'],
                            'subject_paths': ['a.txt', 'b.txt'], 'requirements': ['R1', 'R2']}]} 
        store.declare_validation_plan(plan)
        batch = run_batch(plan, stage)
        self.assertEqual(batch['status'], 'passed')
        verified = store.mark_verified(frozen['snapshot_digest'], batch['report'])
        self.assertEqual(verified['snapshot_digest'], frozen['snapshot_digest'])
        self.assertEqual({r['state'] for r in store.status()['tasks'].values()}, {'verified'})

    def test_validation_plan_rejects_weakening_but_accepts_added_checks(self):
        self.repository()
        store = self.store()
        base = {'requirements': ['R1', 'R2'], 'mandatory_checks': ['required'],
                'checks': [{'id': 'required', 'argv': [sys.executable, '-c', 'print("checked")'],
                            'subject_paths': ['outside.txt'], 'requirements': ['R1', 'R2']}]}
        store.declare_validation_plan(base)
        changed = copy.deepcopy(base)
        changed['checks'][0]['argv'][-1] = 'print("weaker")'
        with self.assertRaisesRegex(ValueError, 'weakens'):
            store.declare_validation_plan(changed)
        changed = copy.deepcopy(base)
        changed['mandatory_checks'] = []
        with self.assertRaisesRegex(ValueError, 'weakens'):
            store.declare_validation_plan(changed)
        added = copy.deepcopy(base)
        added['checks'].append({'id': 'boundary', 'argv': [sys.executable, '-c', 'print("extra")'],
                                'subject_paths': ['outside.txt'], 'requirements': ['R1']})
        added['mandatory_checks'].append('boundary')
        self.assertEqual(store.declare_validation_plan(added)['checks'], 2)
        with self.assertRaisesRegex(ValueError, 'weakens'):
            store.declare_validation_plan(base)

    def test_interface_change_cancels_consumer_and_stale_submission_is_rejected(self):
        self.repository()
        tasks = self.spec()['tasks']
        tasks[1]['depends_on'] = [{'task_id': 'A', 'needs': 'contract_ready', 'interface_id': 'api'}]
        store = self.store(self.spec(tasks=tasks))
        producer = store.dispatch('director', self.limits())[0]
        self.assertEqual(producer['id'], 'A')
        store.publish_interface('A', producer['generation'], 'api', 'v1', {'shape': 'one'})
        consumer = store.dispatch('director', self.limits())[0]
        self.assertEqual(consumer['id'], 'B')
        store.publish_interface('A', producer['generation'], 'api', 'v2', {'shape': 'two'})
        self.assertEqual(store.status()['tasks']['B']['state'], 'cancel_requested')
        store.confirm_cancel('B', consumer['generation'], {'kind': 'not_launched'})
        newer = store.dispatch('director', self.limits())[0]
        self.assertEqual(newer['id'], 'B')
        self.assertGreater(newer['generation'], consumer['generation'])
        with self.assertRaisesRegex(ValueError, 'Stale task generation'):
            store.submit('B', consumer['generation'], git(Path(consumer['workspace']), 'rev-parse', 'HEAD'))

    def test_shared_file_transfers_only_after_staged_artifact(self):
        self.repository()
        tasks = self.spec()['tasks']
        tasks[1]['write_paths'] = ['a.txt']
        tasks[1]['depends_on'] = [{'task_id': 'A', 'needs': 'artifact_ready'}]
        store = self.store(self.spec(tasks=tasks))
        first = store.dispatch('director', self.limits())[0]
        self.assertEqual(first['id'], 'A')
        one = Path(first['workspace'])
        (one / 'a.txt').write_text('first\n', encoding='utf-8')
        git(one, 'add', 'a.txt')
        git(one, 'commit', '-qm', 'first')
        store.submit('A', first['generation'], git(one, 'rev-parse', 'HEAD'))
        store.stage('A')
        second = store.dispatch('director', self.limits())[0]
        self.assertEqual(second['id'], 'B')
        two = Path(second['workspace'])
        self.assertEqual((two / 'a.txt').read_text(), 'first\n')
        (two / 'a.txt').write_text('second\n', encoding='utf-8')
        git(two, 'add', 'a.txt')
        git(two, 'commit', '-qm', 'second')
        store.submit('B', second['generation'], git(two, 'rev-parse', 'HEAD'))
        store.stage('B')
        frozen = store.freeze()
        self.assertEqual((Path(frozen['staging_workspace']) / 'a.txt').read_text(), 'second\n')

    def test_ownership_and_dirty_user_work_are_preserved(self):
        self.repository()
        (self.root / 'outside.txt').write_text('dirty user data\n', encoding='utf-8')
        store = self.store()
        card = store.dispatch('director', self.limits())[0]
        worker = Path(card['workspace'])
        (worker / 'outside.txt').write_text('intrusion\n', encoding='utf-8')
        git(worker, 'add', 'outside.txt')
        git(worker, 'commit', '-qm', 'outside scope')
        with self.assertRaisesRegex(ValueError, 'unowned path'):
            store.submit(card['id'], card['generation'], git(worker, 'rev-parse', 'HEAD'))
        self.assertEqual((self.root / 'outside.txt').read_text(), 'dirty user data\n')

    def test_intent_update_quarantines_then_reuses_only_unchanged_staged_patches(self):
        self.repository()
        store = self.store()
        for card in store.dispatch('director', self.limits()):
            worker = Path(card['workspace'])
            filename = card['id'].lower() + '.txt'
            (worker / filename).write_text(card['id'] + '\n', encoding='utf-8')
            git(worker, 'add', filename)
            git(worker, 'commit', '-qm', card['id'])
            store.submit(card['id'], card['generation'], git(worker, 'rev-parse', 'HEAD'))
            store.stage(card['id'])
        first = store.freeze()
        new_spec = self.spec(revision=2)
        store.invalidate_intent(new_spec['contract_hash'], 2)
        self.assertEqual(store.dispatch('director', self.limits()), [])
        store.amend(new_spec)
        second = store.freeze()
        self.assertNotEqual(first['snapshot_digest'], second['snapshot_digest'])
        self.assertEqual({r['state'] for r in store.status()['tasks'].values()}, {'validation_pending'})
        with self.assertRaisesRegex(ValueError, 'not current'):
            store.assert_snapshot(first['snapshot_digest'])

    def test_actual_process_exit_is_required_before_cancel_reassignment(self):
        self.repository()
        store = self.store()
        card = store.dispatch('director', self.limits(1))[0]
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)'])
        self.addCleanup(lambda: proc.poll() is None and proc.terminate())
        nonce = uuid.uuid4().hex
        store.attach_process(card['id'], card['generation'], proc.pid, nonce)
        store.request_cancel(card['id'])
        with self.assertRaisesRegex(ValueError, 'termination is observed'):
            store.confirm_cancel(card['id'], card['generation'])
        exit_code = proc.wait(timeout=5)
        store.record_termination(card['id'], card['generation'], nonce, exit_code)
        store.confirm_cancel(card['id'], card['generation'])
        self.assertEqual(store.status()['tasks'][card['id']]['state'], 'needs_repair')

    def test_non_git_documents_use_real_isolated_file_workspace(self):
        (self.root / 'input.txt').write_text('source material\n', encoding='utf-8')
        tasks = [{'id': 'A', 'package': 'report', 'objective': 'Write report',
                  'requirements': ['R1'], 'read_paths': ['input.txt'],
                  'write_paths': ['report.txt']}]
        store = self.store(self.spec(tasks=tasks, requirement_ids=('R1',)))
        self.assertEqual(store.status()['mode'], 'files')
        card = store.dispatch('director', self.limits())[0]
        worker = Path(card['workspace'])
        self.assertEqual((worker / 'input.txt').read_text(), 'source material\n')
        (worker / 'report.txt').write_text('grounded report\n', encoding='utf-8')
        store.submit('A', card['generation'])
        store.stage('A')
        frozen = store.freeze()
        self.assertEqual((Path(frozen['staging_workspace']) / 'report.txt').read_text(), 'grounded report\n')
        self.assertEqual((self.root / 'input.txt').read_text(), 'source material\n')
        self.assertFalse((self.root / 'report.txt').exists())


if __name__ == '__main__':
    unittest.main()
