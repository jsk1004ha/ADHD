from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adhd.core import digest, file_hash
from adhd.large_tasks import LargeTaskStore
from adhd.large_execution import run_workers
from adhd.large_native import attach, candidate_evidence
from adhd.snapshots import build_snapshot, validate_snapshot, SnapshotPolicy
from adhd.validation_batch import run_batch
from tests import test_native as native_fixture

ROOT = Path(__file__).resolve().parents[1]


class LargeIntegrationTests(unittest.TestCase):
    def git(self, root, *args):
        result = subprocess.run(['git', '-C', str(root), '-c', 'commit.gpgsign=false',
                                 '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', *args],
                                capture_output=True, check=True)
        return result.stdout.decode().strip()

    def spec(self, run='process-run', revision=1, contract=None, original=None):
        text = original or 'Create two complete parts and preserve their Unicode output.'
        return {'run_id': run, 'contract_revision': revision, 'contract_hash': contract or digest(text),
                'original_turns': [{'id': 'U1', 'text': text}],
                'requirements': [{'id': 'R1', 'text': 'Both complete parts', 'source_turn_id': 'U1', 'source_excerpt': text}],
                'tasks': [{'id': name, 'package': name, 'objective': 'Produce ' + name,
                           'requirements': ['R1'], 'write_paths': [name + '.txt']} for name in ['A', 'B']]}

    def test_real_concurrent_processes_worktrees_assembly_and_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            self.git(ws, 'init', '-q')
            (ws / '.gitignore').write_text('.adhd/\n', encoding='utf-8')
            self.git(ws, 'add', '.')
            self.git(ws, 'commit', '-qm', 'base')
            store = LargeTaskStore(ws, 'process-run')
            store.initialize(self.spec())
            binary = [sys.executable, str(ROOT / 'tests/fake_large_worker.py')]
            with patch('adhd.large_execution.preflight', return_value=binary):
                result = run_workers(store, limits={k: 2 for k in ('host_slots','policy_slots','resource_slots','budget_slots')})
            self.assertEqual([r['status'] for r in result['outcomes']], ['provisionally_staged'] * 2)
            spans = []
            for row in result['outcomes']:
                log = Path(row['logs']).with_suffix('.stdout.jsonl')
                entries = [json.loads(v) for v in log.read_text().splitlines()]
                spans.append((entries[0]['time'], entries[-1]['time']))
                self.assertIsNone(row['usage'])
            self.assertLess(max(s[0] for s in spans), min(s[1] for s in spans))
            frozen = store.freeze()
            self.assertFalse((ws / 'A.txt').exists())
            stage = Path(frozen['staging_workspace'])
            spec = {'run_id': store.run_id, 'contract_revision': 1, 'contract_hash': frozen['contract_hash'],
                    'assembly_digest': frozen['snapshot_digest'], 'snapshot_paths': list(frozen['files']),
                    'requirements': ['R1'], 'mandatory_checks': ['both'], 'checks': [
                        {'id': 'both', 'argv': [sys.executable, '-c', 'from pathlib import Path; assert Path("A.txt").exists() and Path("B.txt").exists()'],
                         'subject_paths': ['A.txt','B.txt'], 'requirements': ['R1']}]}
            store.declare_validation_plan(spec)
            batch = run_batch(spec, stage)
            self.assertEqual(batch['status'], 'passed')
            store.mark_verified(frozen['snapshot_digest'], batch['report'])
            self.assertTrue(all(r['state'] == 'verified' for r in store.status()['tasks'].values()))

    def test_native_attachment_rejects_unverified_parts_and_accepts_exact_batch(self):
        fixture = native_fixture.NativeTests(methodName='test_idle_does_not_loop')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.begin('artifact')
        state = fixture.state()
        original = state['prompts'][0]
        spec = self.spec(state['run_id'], state['intent_version'], state['contract_hash'], original['text'])
        spec['original_turns'][0]['id'] = original['turn_id']
        spec['requirements'][0]['source_turn_id'] = original['turn_id']
        spec['tasks'] = spec['tasks'][:1]
        store = LargeTaskStore(fixture.ws, state['run_id'])
        store.initialize(spec)
        self.assertTrue(fixture.request('attach-large', {'run_id': state['run_id']})['ok'])
        state = fixture.state()
        with self.assertRaises(ValueError):
            candidate_evidence(state, {'large_task_evidence': {'run_id': state['run_id']}})
        card = store.dispatch('director')[0]
        (Path(card['workspace']) / 'A.txt').write_text('Unicode 한글\n', encoding='utf-8')
        store.submit(card['id'], card['generation'], owner=card['owner'])
        store.stage(card['id'])
        frozen = store.freeze()
        stage = Path(frozen['staging_workspace'])
        plan = {'run_id': state['run_id'], 'contract_revision': state['intent_version'],
                          'contract_hash': state['contract_hash'], 'assembly_digest': frozen['snapshot_digest'],
                          'snapshot_paths': list(frozen['files']), 'requirements':['R1'], 'checks':[
                              {'id':'output','argv':[sys.executable,'-c','from pathlib import Path; assert "한글" in Path("A.txt").read_text(encoding="utf-8")'],
                               'subject_paths':['A.txt'],'requirements':['R1']}]}
        store.declare_validation_plan(plan)
        batch = run_batch(plan, stage)
        store.mark_verified(frozen['snapshot_digest'], batch['report'])
        payload = {'large_task_evidence': {'run_id': state['run_id'], 'snapshot_digest': frozen['snapshot_digest'],
                                          'batch_report': batch['report']}}
        proof, _ = candidate_evidence(state, payload)
        self.assertEqual(proof['snapshot_digest'], frozen['snapshot_digest'])
        (fixture.ws / 'answer.txt').write_text('Delivered output', encoding='utf-8')
        candidate = {**payload, 'files':['answer.txt'], 'criterion_results':[{'id':'R1','pass':True,'evidence':'Exact frozen batch and real Unicode output'}], 'sources':[], 'procedure':[]}
        ack = fixture.request('candidate', candidate)
        self.assertTrue(ack['ok'], ack)
        fixture.spawn()
        fixture.review()
        self.assertEqual(fixture.state()['status'], 'complete')
        self.assertEqual(store.status()['tasks']['A']['state'], 'accepted')

    def test_large_bundle_empty_sources_many_leaves_and_stale_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            stage = ws / 'stage'
            stage.mkdir()
            files = {}
            for number in range(160):
                path = stage / (str(number) + '.py')
                path.write_text('' if number == 0 else '# source\n', encoding='utf-8')
                files[path.name] = file_hash(path)
            frozen = {'staging_workspace':str(stage), 'files':files}
            frozen['snapshot_digest'] = digest(frozen)
            (ws/'frozen.json').write_text(json.dumps(frozen), encoding='utf-8')
            snapshot = build_snapshot(ws, [{'kind':'task_bundle','manifest':'frozen.json'}])
            self.assertEqual(validate_snapshot(ws,snapshot)['leaf_count'],161)
            with self.assertRaises(ValueError):
                build_snapshot(ws,[{'kind':'task_bundle','manifest':'frozen.json'}],policy=SnapshotPolicy(max_task_bundle_files=100))
            (stage/'1.py').write_text('changed',encoding='utf-8')
            with self.assertRaises(ValueError):
                validate_snapshot(ws,snapshot)

    def test_actual_cli_batch_reports_failure_exit_and_full_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            (ws/'subject.txt').write_text('real input',encoding='utf-8')
            spec = {'run_id':'cli-run','contract_revision':1,'requirements':['R1'],'checks':[
                {'id':'failure','argv':[sys.executable,'-c','raise RuntimeError("observable failure")'],
                 'subject_paths':['subject.txt'],'requirements':['R1']}]}
            path=ws/'plan.json'
            path.write_text(json.dumps(spec),encoding='utf-8')
            cli=subprocess.run([sys.executable,str(ROOT/'adhd.py'),'batch','run','--workspace',str(ws),'--spec-file',str(path)],capture_output=True)
            self.assertEqual(cli.returncode,2,cli.stderr.decode())
            result=json.loads(cli.stdout)
            self.assertEqual(result['status'],'needs_repair')
            self.assertTrue((ws/result['report']).is_file())


if __name__ == '__main__':
    unittest.main()
