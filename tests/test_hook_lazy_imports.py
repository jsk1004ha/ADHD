"""Optional validators load on actual first use; ordinary processing still persists."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, os, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    ws = base / 'workspace'; ws.mkdir()
    os.environ.update(CODEX_HOME=str(base/'codex'), ADHD_HOME=str(base/'codex/adhd'),
                      ADHD_EXEC_OWNER='', ADHD_HOOK_DIAGNOSTICS='0')
    from adhd import native
    def loaded():
        return {name: 'adhd.'+name in sys.modules for name in ('coding_scope','snapshots','provenance')}
    before = loaded()
    key = native.session_key('lazy-import-tests')
    counter = 0
    def event(name, **fields):
        global counter
        counter += 1
        return native.handle_event(dict(hook_event_name=name, session_id='lazy-import-tests',
                                       cwd=str(ws), turn_id=str(counter), **fields))
    def state():
        return json.loads((native.folder(key)/'state.json').read_text())
    def request(operation, payload):
        queued = native.submit_request(key, ws, operation, payload)
        event('PostToolUse', tool_name='exec_command')
        return json.loads(Path(queued['receipt']).read_text())
    case = sys.argv[1]
    result = {'before':before, 'lock_loaded':'filelock' in sys.modules}
    if case != 'import':
        event('SessionStart', source='startup')
        event('UserPromptSubmit', prompt='Original intent')
        if case == 'idle':
            result['quiet'] = event('UserPromptSubmit', prompt='Second intent')
            event('PostToolUse', tool_name='exec_command')
            event('Stop')
            result['prompts'] = [row['text'] for row in state()['prompts']]
        else:
            mode = 'coding' if case == 'begin' else 'research' if case == 'provenance-error' else 'report'
            kind = 'provenance' if case == 'provenance-error' else 'behavior'
            result['begin'] = request('begin', dict(mode=mode, execution_profile='simple',
                artifacts=['result.txt'], criteria=[dict(id='R1',text='Verified result',kind=kind)]))
            result['after_begin'] = loaded()
            if case != 'begin':
                (ws/'result.txt').write_text('verified result')
                candidate = dict(files=['result.txt'], criterion_results=[
                    dict(id='R1',pass_=True,evidence='Result inspected')])
                candidate['criterion_results'][0]['pass'] = candidate['criterion_results'][0].pop('pass_')
                if case == 'candidate-error':
                    (ws/'result.txt').unlink()
                if case == 'provenance-error':
                    from adhd.evidence import run_check
                    receipt = run_check(dict(run_id=state()['run_id'],contract_revision=state()['intent_version'],
                        subject_paths=['result.txt'],argv=[sys.executable,'-c',
                        'from pathlib import Path; assert Path("result.txt").read_text()=="verified result"']),ws)
                    candidate['criterion_results'][0]['evidence_ids'] = [receipt['receipt']]
                    (ws/'bad.json').write_text('{"schema":-1}')
                    candidate['provenance_manifests'] = [dict(criterion_id='R1',manifest='bad.json')]
                result['candidate'] = request('candidate', candidate)
                if case == 'candidate':
                    result['fresh_before'] = native.is_fresh(state())
                    (ws/'result.txt').write_text('later mutation')
                    result['fresh_after'] = native.is_fresh(state())
            result['processed'] = len(state()['processed'])
        result['status'] = state()['status']
        result['saved_original'] = state()['prompts'][0]['text']
    result['after'] = loaded()
    print(json.dumps(result))
'''


class HookLazyImportTests(unittest.TestCase):
    def child(self, case):
        process = subprocess.run([sys.executable, '-c', CHILD, case], cwd=ROOT,
                                 env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                 capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, '')
        return json.loads(process.stdout)

    def assert_not_loaded(self, result):
        self.assertEqual(result, dict(coding_scope=False, snapshots=False, provenance=False))

    def test_native_import_keeps_optional_validators_unloaded(self):
        result = self.child('import')
        self.assertTrue(result['lock_loaded'])
        self.assert_not_loaded(result['before'])
        self.assert_not_loaded(result['after'])

    def test_idle_events_preserve_originals_and_persist_without_optional_validators(self):
        result = self.child('idle')
        self.assert_not_loaded(result['before'])
        self.assert_not_loaded(result['after'])
        self.assertEqual(result['quiet'], {})
        self.assertEqual(result['prompts'], ['Original intent', 'Second intent'])
        self.assertEqual(result['status'], 'idle')

    def test_coding_begin_loads_scope_and_acknowledges_inbox(self):
        result = self.child('begin')
        self.assert_not_loaded(result['before'])
        self.assertTrue(result['begin']['ok'])
        self.assertEqual(result['after'], dict(coding_scope=True, snapshots=False, provenance=False))
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['status'], 'working')
        self.assertEqual(result['saved_original'], 'Original intent')

    def test_candidate_loads_snapshot_and_detects_later_mutation(self):
        result = self.child('candidate')
        self.assert_not_loaded(result['after_begin'])
        self.assertTrue(result['candidate']['ok'])
        self.assertEqual(result['after'], dict(coding_scope=False, snapshots=True, provenance=False))
        self.assertTrue(result['fresh_before'])
        self.assertFalse(result['fresh_after'])
        self.assertEqual(result['status'], 'reviewing')

    def test_first_snapshot_error_remains_a_failed_ack_and_preserves_state(self):
        result = self.child('candidate-error')
        self.assert_not_loaded(result['after_begin'])
        self.assertFalse(result['candidate']['ok'])
        self.assertTrue(result['after']['snapshots'])
        self.assertEqual(result['status'], 'working')
        self.assertEqual(result['saved_original'], 'Original intent')
        self.assertEqual(result['processed'], 2)

    def test_first_provenance_error_is_validated_and_preserves_original(self):
        result = self.child('provenance-error')
        self.assert_not_loaded(result['after_begin'])
        self.assertFalse(result['candidate']['ok'])
        self.assertIn('Unsupported provenance manifest schema', result['candidate']['message'])
        self.assertTrue(result['after']['provenance'])
        self.assertEqual(result['status'], 'working')
        self.assertEqual(result['saved_original'], 'Original intent')
