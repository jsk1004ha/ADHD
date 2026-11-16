"""Outcome-first execution still requires actual, current results."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import read_json, file_hash, store, discover_skills
from adhd.evidence import run_check
from adhd.models import execution_profile
from adhd.native import folder, handle_event, session_key, submit_request, migrate_state
from adhd.request_routing import classify_request


class OutcomeFirstTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / 'workspace'
        self.ws.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(Path(tmp.name) / 'codex'),
                                   'ADHD_HOME': str(Path(tmp.name) / 'state'), 'ADHD_EXEC_OWNER': ''})
        env.start()
        self.addCleanup(env.stop)
        self.sid = uuid.uuid4().hex
        self.key = session_key(self.sid)
        self.turn = 0
        self.event('UserPromptSubmit', prompt='Implement result.txt and verify it')

    def event(self, name, **kw):
        self.turn += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.turn), **kw})

    def request(self, op, payload):
        queued = submit_request(self.key, self.ws, op, payload)
        self.event('PostToolUse', tool_name='Bash')
        return read_json(Path(queued['receipt']))

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def begin(self, **kw):
        result = self.request('begin', {'mode': 'coding',
            'criteria': [{'id': 'R1', 'text': 'Result works', 'kind': 'test'}],
            'artifacts': ['result.txt'], **kw})
        self.assertTrue(result['ok'], result)

    def check(self, *, failure=False):
        (self.ws / 'result.txt').write_text('working', encoding='utf-8')
        return run_check({'run_id': self.state()['run_id'],
            'contract_revision': self.state()['intent_version'],
            'subject_paths': ['result.txt'],
            'argv': [sys.executable, '-c',
                     'from pathlib import Path; assert Path("result.txt").read_text() == "working"; '
                     + ('raise AssertionError("broken")' if failure else 'print("PASS")')]}, self.ws)

    def candidate(self, receipt):
        return self.request('candidate', {'files': ['result.txt'],
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Executed content assertion',
                                   'evidence_ids': [receipt['receipt']]}]})

    def test_default_finishes_with_current_check_without_review_child(self):
        self.begin()
        self.assertEqual(self.state()['execution_profile']['name'], 'auto')
        result = self.candidate(self.check())
        self.assertTrue(result['ok'], result)
        state = self.state()
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(state['total_children'], 0)
        self.assertEqual(state['completion_receipt']['method'], 'checked')
        self.assertNotIn('review_receipt', state)
        skill = store() / 'recipes' / state['recipe_saved'] / 'SKILL.md'
        self.assertTrue(skill.is_file())
        self.assertIn(str(skill), [row['path'] for row in discover_skills(self.ws)])
        self.assertEqual(state['learning']['method'], 'checked')
        stopped = self.event('Stop')
        self.assertFalse(stopped.get('continue', True))
        self.assertNotIn('independent review', stopped.get('stopReason', ''))

    def test_failed_and_stale_checks_cannot_complete(self):
        self.begin()
        self.assertFalse(self.candidate(self.check(failure=True))['ok'])
        passed = self.check()
        (self.ws / 'result.txt').write_text('broken', encoding='utf-8')
        self.assertFalse(self.candidate(passed)['ok'])
        self.assertNotEqual(self.state()['status'], 'complete')

    def test_status_and_approval_do_not_invalidate_checked_result(self):
        self.begin()
        self.assertTrue(self.candidate(self.check())['ok'])
        before = self.state()['candidate']['digest']
        for prompt in ['진행 상황 알려줘', '좋아', '계속 진행해']:
            self.event('UserPromptSubmit', prompt=prompt)
            self.assertEqual(self.state()['pending_turn_ids'], [])
            self.assertEqual(self.state()['candidate']['digest'], before)

    def test_feedback_changes_only_requested_requirement_without_replanning(self):
        self.begin()
        original = self.state()['contract']
        self.event('UserPromptSubmit', prompt='결과에 한글도 지원해줘')
        state = self.state()
        amended = self.request('sync-intent', {'source_turn_id': state['pending_turn_ids'][0],
            'base_revision': state['intent_version'], 'classification': 'amend',
            'operations': [{'op': 'replace', 'target': 'criteria/R1',
                            'value': {'id': 'R1', 'text': 'Result supports Korean', 'kind': 'test'}}]})
        self.assertTrue(amended['ok'], amended)
        self.assertEqual(self.state()['contract']['artifacts'], original['artifacts'])
        self.assertFalse(self.state()['plan_required'])
        self.assertTrue(self.candidate(self.check())['ok'])
        self.assertEqual(self.state()['status'], 'complete')

    def test_git_auto_does_not_require_separate_scope_audit(self):
        subprocess.run(['git', 'init', '-q', str(self.ws)], check=True)
        self.begin()
        self.assertTrue(self.candidate(self.check())['ok'])
        self.assertEqual(self.state()['status'], 'complete')

    def test_explicit_review_remains_available(self):
        self.begin(execution_profile='standard')
        self.assertTrue(self.state()['plan_required'])
        self.assertFalse(self.candidate(self.check())['ok'])

    def test_protected_original_still_blocks_completion(self):
        (self.ws / 'original.txt').write_text('original', encoding='utf-8')
        self.begin(protected_inputs=['original.txt'])
        receipt = self.check()
        (self.ws / 'original.txt').write_text('changed', encoding='utf-8')
        self.assertFalse(self.candidate(receipt)['ok'])

    def test_default_profile_and_router_are_lightweight(self):
        self.assertEqual(execution_profile()['name'], 'auto')
        for prompt in ['Fix login bug', 'Research the architecture', '이 파일의 오류를 고쳐줘']:
            self.assertEqual(classify_request(prompt)['suggested_profile'], 'auto')

    def test_coding_behavior_cannot_complete_from_prose_alone(self):
        self.begin(criteria=[{'id': 'R1', 'text': 'Result works', 'kind': 'behavior'}])
        (self.ws / 'result.txt').write_text('unverified', encoding='utf-8')
        result = self.request('candidate', {'files': ['result.txt'], 'criterion_results': [
            {'id': 'R1', 'pass': True, 'evidence': 'I think it works'}]})
        self.assertFalse(result['ok'])
        self.assertTrue(self.candidate(self.check())['ok'])

    def test_independent_review_can_be_requested_during_auto_work(self):
        self.begin()
        receipt = self.check()
        payload = {'files': ['result.txt'], 'independent_review': 'yes',
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Executed assertion',
                                   'evidence_ids': [receipt['receipt']]}]}
        self.assertFalse(self.request('candidate', payload)['ok'])
        payload['independent_review'] = True
        self.assertTrue(self.request('candidate', payload)['ok'])
        self.assertEqual(self.state()['status'], 'reviewing')
        self.assertNotIn('completion_receipt', self.state())

    def test_failure_repair_loop_needs_no_plan_ceremony(self):
        self.begin()
        for _ in range(2):
            failed = self.check(failure=True)
            result = self.request('checkpoint', {'summary': 'Assertion failed',
                'next_action': 'Repair the assertion condition and recheck',
                'failure': {'category': 'test_failure', 'detail': 'broken assertion',
                            'evidence_id': failed['receipt']}})
            self.assertTrue(result['ok'], result)
        self.assertFalse(self.state()['needs_replan'])
        self.assertTrue(self.candidate(self.check())['ok'])
        self.assertEqual(self.state()['status'], 'complete')
        self.assertEqual(len(self.state()['failure_history']), 2)

    def test_legacy_v1_profile_is_not_silently_relaxed(self):
        state = self.state()
        state['schema_version'] = 1
        state.pop('execution_profile')
        migrate_state(state, folder(self.key))
        self.assertEqual(state['execution_profile']['name'], 'standard')

    def test_auto_document_needs_current_all_page_review(self):
        try:
            import fitz
        except ImportError:
            self.skipTest('Document rendering dependency is unavailable')
        from adhd.documents import render_document
        document = fitz.open()
        document.new_page().insert_text((40, 40), 'One page')
        document.save(self.ws / 'result.pdf')
        document.close()
        render_document(self.ws / 'result.pdf', self.ws / 'render')
        self.begin(mode='report', artifacts=['result.pdf'],
                   criteria=[{'id': 'R1', 'text': 'Readable PDF', 'kind': 'visual'}])
        payload = {'files': ['result.pdf'],
            'criterion_results': [{'id': 'R1', 'pass': True, 'evidence': 'Rendered PDF'}],
            'document_evidence': [{'path': 'result.pdf', 'render_manifest': 'render/render.json'}]}
        self.assertFalse(self.request('candidate', payload)['ok'])
        payload['document_reviews'] = [{'path': 'result.pdf',
            'render_sha256': file_hash(self.ws / 'render/render.json'),
            'inspected_pages': [], 'pass': True, 'evidence': 'Test double for page inspection'}]
        self.assertFalse(self.request('candidate', payload)['ok'])
        payload['document_reviews'][0]['inspected_pages'] = [1]
        self.assertTrue(self.request('candidate', payload)['ok'])
        self.assertEqual(self.state()['candidate']['document_reviews'], payload['document_reviews'])
        self.assertEqual(self.state()['candidate']['document_evidence'][0]['visual_approval'], 'parent_checked')

    def test_feedback_reuses_only_unchanged_requirement_and_current_inputs(self):
        self.begin(artifacts=['a.txt','b.txt'], criteria=[
            {'id':'R1','text':'A works','kind':'test'}, {'id':'R2','text':'B works','kind':'test'}])
        def check_file(name, value):
            (self.ws / name).write_text(value, encoding='utf-8')
            return run_check({'run_id':self.state()['run_id'],
                'contract_revision':self.state()['intent_version'], 'subject_paths':[name],
                'argv':[sys.executable,'-c',
                        f'from pathlib import Path; assert Path({name!r}).read_text() == {value!r}']},self.ws)['receipt']
        def submit(a, b):
            return self.request('candidate', {'files':['a.txt','b.txt'], 'criterion_results':[
                {'id':'R1','pass':True,'evidence':'A assertion passed','evidence_ids':[a]},
                {'id':'R2','pass':True,'evidence':'B assertion passed','evidence_ids':[b]}]})
        a, b = check_file('a.txt','one'), check_file('b.txt','two')
        self.assertTrue(submit(a,b)['ok'])
        old_revision=self.state()['intent_version']
        self.event('UserPromptSubmit',prompt='Change only the A output to three')
        state=self.state()
        self.assertTrue(self.request('sync-intent', {'source_turn_id':state['pending_turn_ids'][0],
            'base_revision':state['intent_version'],'classification':'amend','operations':[
                {'op':'replace','target':'criteria/R1','value':{'id':'R1','text':'A is three','kind':'behavior'}}]})['ok'])
        self.assertFalse(submit(a,b)['ok'])  # The changed requirement cannot reuse old proof.
        self.assertFalse(self.request('candidate', {'files':['a.txt','b.txt'], 'criterion_results':[
            {'id':'R1','pass':True,'evidence':'A merely claimed to work'},
            {'id':'R2','pass':True,'evidence':'Previous B check','evidence_ids':[b]}]})['ok'])
        fresh_a=check_file('a.txt','three')
        (self.ws/'b.txt').write_text('unexpected change',encoding='utf-8')
        self.assertFalse(submit(fresh_a,b)['ok'])  # Unchanged requirement still needs current bytes.
        (self.ws/'b.txt').write_text('two',encoding='utf-8')
        result=submit(fresh_a,b)
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.state()['candidate']['execution_revisions'][b],old_revision)
        self.assertFalse(self.event('Stop').get('continue',True))


if __name__ == '__main__':
    unittest.main()
