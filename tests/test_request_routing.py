"""Real request boundaries and native hook routing lifecycle."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.core import atomic_json, read_json
from adhd.native import folder, handle_event, session_key, submit_request
from adhd.request_routing import classify_request, is_status_followup


class RequestRoutingTests(unittest.TestCase):
    def test_conservative_boundaries_in_korean_and_english(self):
        direct = ['안녕', 'status?', 'Translate "hello" to Korean',
                  '배포가 뭐야?', 'explain deleting files', 'Translate: fix login',
                  'Translate: "this file"', 'Translate: "delete all files"',
                  'Translate: "report.hwpx"', 'Translate: "docs/report.xlsx"',
                  'Translate: "it"',
                  '다음 문장 오타 수정해줘: "안녕하새요"']
        inspect = ['로그인 고쳐줘', 'fix login', '이 함수 왜 느려?',
                   'Why is this function slow?', '간단히 DB 삭제해줘',
                   'fix the payment flow', 'Update the project',
                   '이 보고서를 완성해줄래?', '번역하고 PDF로 저장해줘',
                   'Explain this concept then implement the whole system',
                   '이 파일에 전체 기능 구현해', 'Translate: this file',
                   'Translate this file', '번역: 이 파일', 'Translate:',
                   'Translate', 'Translate to Korean', 'Translate: report.hwpx',
                   'Translate: data.csv', 'Translate: budget.xlsx',
                   'Translate: app.js', 'Translate: config.yaml',
                   'Translate: docs/report', 'Translate: C:\\docs\\report.hwpx',
                   'Translate it to Korean', 'Translate: this',
                   'Translate the attached to English']
        for prompt in direct:
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_request(prompt)['path'], 'direct')
        for prompt in inspect:
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_request(prompt)['path'], 'inspect')
        self.assertEqual(classify_request('Fix this function')['path'], 'simple')
        self.assertEqual(classify_request('Research an architecture migration')['path'], 'deep')
        self.assertEqual(classify_request('"foo" 오타를 프로젝트 전체에서 고쳐줘')['path'], 'inspect')

    def test_approval_inherits_substantive_request_and_active_contract_wins(self):
        route = classify_request('ㅇㅇ', previous='Build a Windows installer and test it')
        self.assertEqual(route['path'], 'inspect')
        self.assertEqual(route['effective_prompt'], 'Build a Windows installer and test it')
        self.assertEqual(classify_request('do it')['path'], 'inspect')
        self.assertEqual(classify_request('do it', previous='status?')['path'], 'inspect')
        self.assertEqual(classify_request('ㅇㅇ 그렇게 해줘', previous='안녕')['path'], 'inspect')
        self.assertEqual(classify_request('ㅇㅇ 그렇게 해줘', previous='Build a Windows installer')['effective_prompt'],
                         'Build a Windows installer')
        self.assertEqual(classify_request('hi', active_contract=True)['path'], 'active')
        for prompt in ('What is the status?', 'status?', '진행 상황 알려줘', '어디까지 했어?'):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_status_followup(prompt))
        self.assertFalse(is_status_followup('배포가 뭐야?'))

    def test_cli_exposes_same_initial_route(self):
        runner = Path(__file__).resolve().parents[1] / 'adhd.py'
        result = subprocess.run([sys.executable, str(runner), 'request-route', 'fix login'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"path": "inspect"', result.stdout)


class NativeRoutingHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ws = Path(temp.name) / 'workspace'
        self.ws.mkdir()
        codex = Path(temp.name) / 'codex'
        codex.mkdir()
        env = patch.dict(os.environ, {'CODEX_HOME': str(codex), 'ADHD_EXEC_OWNER': ''})
        env.start()
        self.addCleanup(env.stop)
        self.sid = uuid.uuid4().hex
        self.key = session_key(self.sid)
        self.turn = 0

    def event(self, name, **extra):
        self.turn += 1
        return handle_event({'hook_event_name': name, 'session_id': self.sid,
                             'cwd': str(self.ws), 'turn_id': str(self.turn), **extra})

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def test_idle_direct_then_substantive_route_emits_new_guidance(self):
        first = self.event('UserPromptSubmit', prompt='안녕')
        self.assertIn('Answer directly', str(first))
        self.assertIn('Profile=simple; plan=optional', str(first))
        self.assertIn('no native plan or acceptance loop', str(first))
        self.assertEqual(self.state()['request_route']['path'], 'direct')
        self.assertNotIn('run_id', self.state())
        self.assertEqual(self.event('UserPromptSubmit', prompt='hello'), {})
        second = self.event('UserPromptSubmit', prompt='로그인 고쳐줘')
        self.assertIn('Inspect the request', str(second))
        self.assertEqual(self.state()['request_route']['path'], 'inspect')
        self.assertEqual(len(self.state()['prompts']), 3)

    def test_idle_approval_preserves_source_and_default_begin_profile(self):
        self.event('UserPromptSubmit', prompt='Fix this function')
        self.event('UserPromptSubmit', prompt='ㅇㅇ 그렇게 해줘')
        route = self.state()['request_route']
        self.assertEqual(route['path'], 'simple')
        self.assertEqual(route['effective_prompt'], 'Fix this function')
        receipt = submit_request(self.key, self.ws, 'begin', {
            'mode': 'research', 'criteria': [{'id': 'R1', 'text': 'Answer the request', 'kind': 'artifact'}],
            'artifacts': ['answer.md']})
        self.event('PostToolUse', tool_name='Bash')
        self.assertTrue(read_json(Path(receipt['receipt']))['ok'])
        state = self.state()
        self.assertEqual(state['execution_profile']['name'], 'simple')
        self.assertEqual([p['text'] for p in state['prompts']], ['Fix this function', 'ㅇㅇ 그렇게 해줘'])

    def test_approval_skips_intervening_small_turns_and_restores_current_objective(self):
        self.event('UserPromptSubmit', prompt='배포가 뭐야?')
        self.event('UserPromptSubmit', prompt='로그인 고쳐줘')
        self.event('UserPromptSubmit', prompt='status?')
        self.event('UserPromptSubmit', prompt='안녕')
        approval = self.event('UserPromptSubmit', prompt='ㅇㅇ 그렇게 해줘')
        self.assertEqual(self.state()['request_route']['effective_prompt'], '로그인 고쳐줘')
        self.assertIn('Objective: 로그인 고쳐줘', str(approval))
        self.assertNotIn('Objective: 배포가 뭐야?', str(approval))
        self.assertEqual(len(self.state()['prompts']), 5)

    def test_approval_without_substantive_source_needs_inspection(self):
        self.event('UserPromptSubmit', prompt='안녕')
        self.event('UserPromptSubmit', prompt='status?')
        approval = self.event('UserPromptSubmit', prompt='do it')
        self.assertEqual(self.state()['request_route']['path'], 'inspect')
        self.assertEqual(self.state()['request_route']['effective_prompt'], 'do it')
        self.assertNotIn('Objective: do it', str(approval))

    def test_completed_contract_allows_new_direct_question_without_amendment(self):
        self.event('UserPromptSubmit', prompt='Fix this function')
        state = self.state()
        state['status'] = 'complete'
        state['contract'] = {'criteria': [{'id': 'R1', 'text': 'Original', 'kind': 'artifact'}]}
        atomic_json(folder(self.key) / 'state.json', state)
        direct = self.event('UserPromptSubmit', prompt='배포가 뭐야?')
        self.assertIn('Answer the new direct request', str(direct))
        self.assertEqual(self.state()['contract'], state['contract'])
        self.assertEqual(self.state()['pending_turn_ids'], [])
        self.event('UserPromptSubmit', prompt='What is the status?')
        self.assertEqual(self.state()['status'], 'complete')
        self.assertEqual(self.state()['request_route']['path'], 'active')
        self.assertTrue(self.state()['pending_turn_ids'])

    def test_explicit_begin_profile_overrides_suggestion_and_followup_stays_pending(self):
        self.event('UserPromptSubmit', prompt='Fix this function')
        receipt = submit_request(self.key, self.ws, 'begin', {
            'mode': 'research', 'execution_profile': 'standard',
            'criteria': [{'id': 'R1', 'text': 'Answer the request', 'kind': 'artifact'}],
            'artifacts': ['answer.md']})
        self.event('PostToolUse', tool_name='Bash')
        self.assertTrue(read_json(Path(receipt['receipt']))['ok'])
        self.assertEqual(self.state()['execution_profile']['name'], 'standard')
        self.event('UserPromptSubmit', prompt='status?')
        self.assertTrue(self.state()['pending_turn_ids'])
        self.assertEqual(self.state()['request_route']['path'], 'active')


if __name__ == '__main__':
    unittest.main()
