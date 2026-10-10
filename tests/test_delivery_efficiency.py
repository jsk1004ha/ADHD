"""Regression fixtures for the delivery-efficiency contract (F1-F13)."""
from __future__ import annotations

import tempfile
import unittest
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from adhd.core import digest
from adhd.intent import apply_intent_patch
from adhd.native import goal_command, migrate_state, initial
from adhd.validation_batch import run_batch, load_report


class DeliveryEfficiencyTests(unittest.TestCase):
    def contract(self):
        return {'intent_version': 1, 'criteria': [
            {'id': 'R1', 'text': 'Preserve CPU/RAM graphs', 'kind': 'behavior'}],
            'artifacts': ['widget.exe'], 'documents': [], 'protected_inputs': {}}

    def amend(self, contract, operations):
        return apply_intent_patch(contract, {'source_turn_id': 'turn-2',
            'base_revision': contract['intent_version'], 'classification': 'amend',
            'operations': operations}, ['turn-2'])[0]

    def test_f2_f3_additive_amend_preserves_existing_ui_and_explicit_retraction(self):
        contract = self.amend(self.contract(), [{'op': 'add', 'target': 'criteria/R2',
            'value': {'id': 'R2', 'text': 'Refresh faster', 'kind': 'behavior'}}])
        self.assertEqual([r['id'] for r in contract['criteria']], ['R1', 'R2'])
        contract = self.amend(contract, [{'op': 'retract', 'target': 'criteria/R1'}])
        self.assertEqual([r['id'] for r in contract['criteria']], ['R2'])

    def test_f4_preservation_and_runtime_metadata_are_requirement_bound(self):
        from adhd.delivery_policy import validate_contract_metadata
        value = {'preserve_conditions': [{'id': 'P1', 'text': 'Windows Python entrypoint',
            'requirement_id': 'R1', 'source_turn_id': 'turn-1', 'basis': 'user'}],
            'runtime_context': {'os': 'Windows', 'entrypoint': 'python adhd.py',
                'requirement_id': 'R1', 'source_turn_id': 'turn-1', 'basis': 'observed'}}
        self.assertEqual(validate_contract_metadata(value, self.contract()['criteria'])['runtime_context']['os'], 'Windows')
        value['runtime_context']['requirement_id'] = 'R999'
        with self.assertRaises(ValueError):
            validate_contract_metadata(value, self.contract()['criteria'])

    def test_f1_scoped_denial_blocks_same_action_without_erasing_user_authorization(self):
        from adhd.execution_decisions import action_key, record_action_outcome, action_decision
        action = {'operation': 'delete', 'canonical_targets': ['C:/fixture/a'],
                  'scope_digest': digest('one'), 'source_turn_id': 'turn-1',
                  'constraints': [], 'authorized': True}
        state = {'actions': {action_key(action): action}}
        record_action_outcome(state, action, 'host_policy_denied', 'host:tool-1', 'policy-v1')
        result = action_decision(state, action, 'policy-v1')
        self.assertFalse(result['retry_allowed'])
        self.assertTrue(state['actions'][action_key(action)]['authorized'])
        self.assertTrue(action_decision(state, action, 'policy-v2')['retry_allowed'])
        record_action_outcome(state, action, 'unknown_denial', 'host:tool-2', None)
        self.assertFalse(action_decision(state, action, 'policy-v3')['retry_allowed'])

    def test_f1_host_denial_is_correlated_without_a_subprocess_receipt(self):
        from adhd.execution_decisions import register_action, observe_action_host_result, action_decision
        request = {'cmd': 'restricted operation'}
        state = {'actions': {}}
        action = register_action(state, {'operation': 'publish', 'canonical_targets': ['demo'],
            'scope_digest': digest('demo'), 'source_turn_id': 'turn-1',
            'authorization_excerpt': 'demo 게시', 'tool_name': 'mcp__host__publish',
            'request_sha256': digest(request), 'constraints': []})
        action['authorized'] = True
        event = {'tool_name': 'mcp__host__publish', 'tool_input': request,
                 'tool_use_id': 'tool-1', 'policy_version': 'v1',
                 'tool_response': {'isError': True, 'message': 'blocked by policy'}}
        self.assertEqual(observe_action_host_result(state, event)['outcome'], 'unknown_denial')
        observe_action_host_result(state, event)
        self.assertEqual(len(action['attempts']), 1)
        self.assertFalse(action_decision(state, action, 'v1')['retry_allowed'])
        self.assertTrue(action_decision(state, action, 'v2')['retry_allowed'])

    def test_f1_os_permission_is_separate_from_host_policy(self):
        from adhd.execution_decisions import classify_denial
        self.assertEqual(classify_denial('Permission denied: C:/fixture'), 'os_permission_denied')
        self.assertEqual(classify_denial({'code': 'policy_denied', 'message': 'blocked by policy'}), 'host_policy_denied')

    def test_f1_brief_approval_needs_one_observed_proposal(self):
        from adhd.execution_decisions import register_action, authorize_action
        proposal = 'Publish demo to the selected destination?'
        state = {'prompts': [{'turn_id': 'turn-2', 'text': '진행해줘', 'time': 20}],
                 'last_assistant_message': {'text': proposal, 'sha256': digest(proposal),
                                            'observed_at': 10}}
        action = register_action(state, {'operation': 'publish', 'canonical_targets': ['demo'],
            'scope_digest': digest('demo'), 'source_turn_id': 'turn-1',
            'authorization_excerpt': 'demo 게시', 'proposal_excerpt': proposal,
            'constraints': []})
        self.assertTrue(authorize_action(state, action['action_id'],
                                         turn_id='turn-2', prompt='진행해줘')['authorized'])
        state['last_assistant_message']['sha256'] = digest('unrelated')
        action['authorized'] = False
        with self.assertRaises(ValueError):
            authorize_action(state, action['action_id'], turn_id='turn-2', prompt='진행해줘')

    def test_f7_second_identical_failure_requires_new_hypothesis(self):
        from adhd.recovery import record_failure
        state = {}
        signature = digest('same assertion and same input')
        first = record_failure(state, 'test_failure', 'Observed assertion', 'check:first',
                               verified_signature=signature)
        second = record_failure(state, 'test_failure', 'Still the same assertion', 'check:second',
                                verified_signature=signature)
        self.assertTrue(first['retry_allowed'])
        self.assertFalse(second['retry_allowed'])
        self.assertIn('hypothesis', second['next_action'])

    def test_f5_push_is_not_live_delivery(self):
        from adhd.delivery_policy import delivery_status
        target = {'kind': 'live_deployment', 'requirement_id': 'R1',
                  'source_turn_id': 'turn-1', 'basis': 'user'}
        self.assertEqual(delivery_status(target, {'push_sha': 'abc'})['status'], 'incomplete')
        self.assertEqual(delivery_status(target, {'publish_ref': 'host:1',
            'verification_ref': 'host:2', 'expected_sha': 'abcdef123', 'live_sha': 'abcdef123',
            'source_paths': ['src/app.py'], 'health': 'healthy'})['status'], 'evidence_pending')

    def test_f6_first_deadline_survives_restatement_and_warns_before_due(self):
        from adhd.delivery_policy import deadline_guidance, merge_deadline
        first = {'due_at': '2026-10-10T10:00:00+09:00', 'timezone': 'Asia/Seoul',
                 'source_turn_id': 'turn-1', 'basis': 'user', 'observed_at': '2026-10-10T09:00:00+09:00',
                 'requirement_id': 'R1'}
        self.assertEqual(merge_deadline(first, {**first, 'source_turn_id': 'turn-2'})['source_turn_id'], 'turn-1')
        self.assertEqual(deadline_guidance(first, '2026-10-10T09:50:00+09:00')['level'], 'high')

    def test_f8_unknown_child_keeps_writer_lease(self):
        from adhd.native import end_after_children
        state = {'children': {'x': {'status': 'unknown'}}, 'workspace': '.', 'key': 'x', 'run_id': 'x'}
        end_after_children(state, 'paused')
        self.assertEqual(state['status'], 'interrupt_pending')

    def test_f9_cumulative_token_reset_and_parallel_wall_time(self):
        from adhd.run_metrics import summarize_run_metrics
        rows = [{'id': 'a', 'kind': 'usage', 'actor': 'parent', 'cumulative': True,
                 'input_tokens': 100, 'cached_input_tokens': 10, 'output_tokens': 20},
                {'id': 'b', 'kind': 'usage', 'actor': 'parent', 'cumulative': True,
                 'input_tokens': 150, 'cached_input_tokens': 15, 'output_tokens': 30},
                {'id': 'c', 'kind': 'usage', 'actor': 'parent', 'cumulative': True,
                 'input_tokens': 20, 'cached_input_tokens': 2, 'output_tokens': 3},
                {'id': 'd', 'kind': 'usage', 'actor': 'child-1', 'cumulative': False,
                 'input_tokens': None, 'cached_input_tokens': None, 'output_tokens': None},
                {'id': 'span', 'kind': 'run', 'started_ms': 0, 'finished_ms': 100}]
        value = summarize_run_metrics(rows + [rows[1]])
        self.assertEqual(value['tokens']['input_tokens']['known'], 170)
        self.assertEqual(value['wall_ms'], 100)
        self.assertGreater(value['tokens']['input_tokens']['missing_events'], 0)
        self.assertEqual(value['reset_segments']['parent'], 2)

    def test_f10_future_reader_guard_accepts_v2_and_rejects_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = initial('key', Path(tmp))
            migrate_state(state, Path(tmp))
            future = {**state, 'schema_version': 99, 'min_reader_version': 99}
            with self.assertRaises(ValueError):
                migrate_state(future, Path(tmp))

    def test_f10_active_install_downgrade_guard(self):
        from adhd.native_install import _assert_active_state_readable
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            directory = parent / 'native' / ('a' * 24)
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text(json.dumps({'status': 'working',
                'schema_version': 3, 'min_reader_version': 3}), encoding='utf-8')
            with self.assertRaises(ValueError):
                _assert_active_state_readable(parent, 2)
            (directory / 'state.json').write_text(json.dumps({'status': 'paused',
                'schema_version': 3, 'min_reader_version': 3}), encoding='utf-8')
            _assert_active_state_readable(parent, 2)

    def test_f10_new_cli_refuses_old_hook_before_queuing_required_metadata(self):
        from adhd.native import submit_request, bridge
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            session = 'a' * 24
            old_view = bridge(ws, session) / 'view.json'
            old_view.parent.mkdir(parents=True)
            old_view.write_text(json.dumps({'status': 'idle', 'schema_version': 2}),
                                encoding='utf-8')
            with self.assertRaises(ValueError):
                submit_request(session, ws, 'begin', {'preserve_conditions': [
                    {'id': 'P1', 'text': 'Keep the graph', 'requirement_id': 'R1',
                     'source_turn_id': 'turn-1', 'basis': 'user'}]})
            self.assertFalse((old_view.parent / 'inbox').exists())

    def test_f10_new_cli_queues_with_current_managed_reader(self):
        from adhd.native import submit_request, bridge
        from adhd.core import ROOT, file_hash
        import adhd.native as native_module
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            session = 'b' * 24
            view = bridge(ws, session) / 'view.json'
            view.parent.mkdir(parents=True)
            view.write_text(json.dumps({'session_reader': {
                'release_root': str(ROOT.resolve()),
                'source_sha256': file_hash(Path(native_module.__file__).resolve()),
                'max_state_version': 2}}), encoding='utf-8')
            with patch('adhd.native_install._find_managed_installation',
                       return_value=(None, None, {'release': str(ROOT.resolve())})):
                queued = submit_request(session, ws, 'begin', {'delivery_target': {'kind': 'release'}})
            self.assertTrue((view.parent / 'inbox' / (queued['queued'] + '.json')).is_file())

    def test_f11_packet_keeps_long_verbatim_and_rejects_wrong_delta_base(self):
        from adhd.large_prompts import plan_context_packet, apply_context_delta
        task = {'task_id': 'T1', 'revision': 2, 'requirement_ids': ['R1'],
                'verbatim_excerpts': ['한글 필수 ' * 900], 'owned_paths': ['adhd/a.py'],
                'dependencies': [], 'acceptance': ['R1'], 'evidence_refs': []}
        packet = plan_context_packet(task, {})
        self.assertEqual(packet['verbatim_excerpts'], task['verbatim_excerpts'])
        with self.assertRaises(ValueError):
            apply_context_delta(packet, {'base_revision': 1, 'revision': 3, 'changed': {}})

    def test_f13_wait_backoff_resets_on_change_and_alive_is_not_failure(self):
        from adhd.execution_decisions import next_wait
        self.assertEqual(next_wait({'unchanged_count': 2, 'alive': True})['seconds'], 30)
        self.assertEqual(next_wait({'unchanged_count': 9, 'changed': True})['seconds'], 5)
        self.assertFalse(next_wait({'unchanged_count': 9, 'alive': True})['failed'])

    def test_f7_environment_and_fixture_change_prevent_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / 'subject.txt').write_text('source', encoding='utf-8')
            (ws / 'fixture.txt').write_text('fixture 1', encoding='utf-8')
            check = {'id': 'focused', 'argv': [sys.executable, '-c', 'print("ok")'],
                     'subject_paths': ['subject.txt'], 'fixture_paths': ['fixture.txt'],
                     'environment_vars': ['ADHD_FIXTURE_REVISION'],
                     'requirements': ['R1'], 'impact_complete': True, 'environment_complete': True}
            spec = {'run_id': 'r1', 'contract_revision': 1, 'contract_hash': 'contract',
                    'requirements': ['R1'], 'mandatory_checks': ['focused'], 'checks': [check]}
            with patch.dict(os.environ, {'ADHD_FIXTURE_REVISION': 'one'}):
                first = run_batch(spec, ws)
                spec['previous_report'] = first['report']
                second = run_batch(spec, ws)
                self.assertEqual(load_report(ws, second['report'])['results'][0]['status'], 'reused')
            with patch.dict(os.environ, {'ADHD_FIXTURE_REVISION': 'two'}):
                third = run_batch(spec, ws)
                self.assertEqual(load_report(ws, third['report'])['results'][0]['status'], 'passed')
            (ws / 'fixture.txt').write_text('fixture 2', encoding='utf-8')
            with patch.dict(os.environ, {'ADHD_FIXTURE_REVISION': 'one'}):
                fourth = run_batch(spec, ws)
                self.assertEqual(load_report(ws, fourth['report'])['results'][0]['status'], 'passed')

    def test_f7_failed_batch_same_inputs_does_not_run_unjustified_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / 'subject.txt').write_text('same input', encoding='utf-8')
            code = ('from pathlib import Path;import sys;'
                    'p=Path("attempts.txt");p.write_text(p.read_text()+"x" if p.exists() else "x");'
                    'sys.exit(1)')
            spec = {'run_id': 'r1', 'contract_revision': 1, 'contract_hash': 'contract',
                    'requirements': ['R1'], 'mandatory_checks': ['failure'],
                    'checks': [{'id': 'failure', 'argv': [sys.executable, '-c', code],
                                'subject_paths': ['subject.txt'], 'requirements': ['R1'],
                                'dependency_paths': [], 'fixture_paths': [],
                                'environment_vars': [], 'impact_complete': True,
                                'environment_complete': True}]}
            first = run_batch(spec, ws)
            spec['previous_report'] = first['report']
            second = run_batch(spec, ws)
            second_report = load_report(ws, second['report'], require_current=False)
            row = second_report['results'][0]
            self.assertEqual((ws / 'attempts.txt').read_text(encoding='utf-8'), 'x')
            self.assertEqual(row['decision_class'], 'unjustified_duplicate_held')
            self.assertEqual(second_report['usage']['check_processes'], 0)
            spec['retry_policy'] = {'failure': {'kind': 'bounded_flake_probe',
                'max_attempts': 1, 'reason': 'A transient runner error is possible',
                'evidence_ref': row['receipt']}}
            third = run_batch(spec, ws)
            self.assertEqual(load_report(ws, third['report'], require_current=False)
                             ['results'][0]['decision_class'], 'bounded_flake_probe')
            spec['previous_report'] = third['report']
            fourth = run_batch(spec, ws)
            self.assertEqual(load_report(ws, fourth['report'], require_current=False)
                             ['results'][0]['decision_class'], 'unjustified_duplicate_held')
            self.assertEqual((ws / 'attempts.txt').read_text(encoding='utf-8'), 'xx')

    def test_f12_build_decision_is_exposed_through_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / 'artifact.zip').write_bytes(b'build')
            manifest = {'artifact': 'artifact.zip', 'source_hashes': {'src': digest('src')},
                        'toolchain_hashes': {'python': digest(sys.version)}, 'flags': [],
                        'dependency_hashes': {'dep': digest('dep')},
                        'environment_hash': digest('env')}
            path = ws / 'manifest.json'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'adhd.py'),
                       'batch', 'build-decision', '--workspace', str(ws), '--spec-file', str(path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['reuse'])

    def test_r8_paired_driver_reports_only_observed_matched_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            gate = {'requirements_passed': True, 'preservation_passed': True,
                    'security_passed': True, 'independent_review_passed': True}
            baseline, candidate = [], []
            for index in range(3):
                common = {'case_id': 'case-' + str(index), 'task_type': 'small',
                          'model': 'same-model', 'effort': 'high', 'scope_hash': digest('scope'),
                          'environment_hash': digest('env'), 'delivery_target': 'local_artifact',
                          'acceptance': gate,
                          'first_usable_ms': 100, 'delivery_ms': 120,
                          'input_tokens': 100, 'cached_input_tokens': 20,
                          'output_tokens': 20, 'duplicate_checks': 2, 'unchanged_polls': 2}
                baseline.append(common)
                candidate.append({**common, 'first_usable_ms': 80, 'delivery_ms': 100,
                                  'input_tokens': 75, 'cached_input_tokens': 15,
                                  'duplicate_checks': 0, 'unchanged_polls': 1})
            base_path, cand_path = ws / 'base.json', ws / 'candidate.json'
            base_path.write_text(json.dumps(baseline), encoding='utf-8')
            cand_path.write_text(json.dumps(candidate), encoding='utf-8')
            driver = Path(__file__).resolve().parents[1] / 'scripts' / 'compare_delivery_efficiency.py'
            command = [sys.executable, str(driver), '--baseline', str(base_path), '--candidate', str(cand_path)]
            run = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(report['by_task_type']['small']['disposition'], 'measured_exploratory')
            self.assertEqual(report['by_task_type']['small']['paired_median_savings']['input_tokens'], .25)
            candidate[0].pop('model')
            cand_path.write_text(json.dumps(candidate), encoding='utf-8')
            run = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 2)
            candidate[0] = {**baseline[0]}
            candidate[0].pop('delivery_target')
            cand_path.write_text(json.dumps(candidate), encoding='utf-8')
            run = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 2)

    def test_r8_executable_failure_policy_benchmark_keeps_unknown_live_metrics(self):
        root = Path(__file__).resolve().parents[1]
        driver = root / 'scripts' / 'benchmark_delivery_efficiency.py'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report.json'
            run = subprocess.run([sys.executable, str(driver), '--baseline-tree', str(root),
                '--candidate-tree', str(root), '--output', str(output)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['workload']['case_id'], 'same-input-failed-check')
            self.assertEqual([row['status'] for row in report['baseline']['runs']],
                             ['needs_repair'] * 5)
            self.assertEqual(report['baseline']['observed_process_starts'], 1)
            self.assertEqual(report['candidate']['observed_process_starts'], 1)
            self.assertEqual(report['baseline']['distinct_receipts'], 1)
            self.assertIsNone(report['live_model_tokens'])
            self.assertIsNone(report['actual_delivery_ms'])
            self.assertLess(report['prompt_payload']['candidate_delta_bytes'],
                            report['prompt_payload']['candidate_full_resend_bytes'])
            self.assertTrue(report['prompt_payload']['original_excerpts_intact'])

    def test_explicit_markdown_skill_link_selects_goal(self):
        self.assertEqual(goal_command('[$adhd-goal](skill://adhd-goal) 완성해 줘'), '완성해 줘')
        self.assertEqual(goal_command(r'[$adhd-goal](C:\Users\js100\.codex\skills\adhd-goal\SKILL.md) 구현시작'), '구현시작')
        self.assertIsNone(goal_command('Explain [$adhd-goal](skill://adhd-goal)'))


class NativeDeliveryFlowTests(unittest.TestCase):
    def setUp(self):
        from adhd.core import ROOT
        from tests.test_native import NativeTests
        self.managed = patch('adhd.native_install._find_managed_installation',
                             return_value=(None, None, {'release': str(ROOT.resolve())}))
        self.managed.start()
        self.native = NativeTests('test_idle_does_not_loop')
        self.native.setUp()

    def tearDown(self):
        self.native.tearDown()
        self.managed.stop()

    def test_f5_new_delivery_contract_requires_batch_and_live_proof(self):
        from adhd.evidence import run_check
        native = self.native
        native.event('SessionStart')
        turn = native.state()['prompts'][-1]['turn_id']
        payload = {'mode': 'coding', 'criteria': [{'id': 'R1', 'text': 'Working output', 'kind': 'test'}],
                   'artifacts': ['answer.txt'],
                   'delivery_target': {'kind': 'live_deployment', 'requirement_id': 'R1',
                       'source_turn_id': turn, 'basis': 'user'},
                   'plan': {'objective': 'Produce and deliver output',
                       'approach': 'Implement then verify', 'verification': 'Run evidence checks',
                       'steps': [{'id': 'S1', 'action': 'Implement and verify',
                                  'requirements': ['R1'], 'depends_on': []}]}}
        self.assertTrue(native.request('begin', payload)['ok'])
        (native.ws / 'answer.txt').write_text('working', encoding='utf-8')
        state = native.state()
        spec = {'run_id': state['run_id'], 'contract_revision': state['intent_version'],
                'contract_hash': state['contract_hash'], 'requirements': ['R1'],
                'mandatory_checks': ['output'], 'checks': [{'id': 'output',
                    'argv': [sys.executable, '-c', 'print("ok")'],
                    'subject_paths': ['answer.txt'], 'requirements': ['R1']}]}
        batch = run_batch(spec, native.ws)
        report = load_report(native.ws, batch['report'])
        criterion_ref = report['results'][0]['receipt']
        criteria = [{'id': 'R1', 'pass': True, 'evidence': 'Observed focused check',
                     'evidence_ids': [criterion_ref]}]
        common = {'files': ['answer.txt'], 'criterion_results': criteria,
                  'sources': [], 'procedure': ['Check observed output']}
        self.assertFalse(native.request('candidate', {**common,
            'delivery_evidence': {'push_sha': 'abcdef1234567890'}})['ok'])
        self.assertTrue(native.request('attach-batch', {'report': batch['report']})['ok'])
        self.assertFalse(native.request('candidate', {**common,
            'delivery_evidence': {'push_sha': 'abcdef1234567890'}})['ok'])
        publish = run_check({'run_id': state['run_id'], 'contract_revision': state['intent_version'],
            'subject_paths': ['answer.txt'], 'argv': [sys.executable, '-c', 'print("publish")']}, native.ws)
        verify = run_check({'run_id': state['run_id'], 'contract_revision': state['intent_version'],
            'subject_paths': ['answer.txt'], 'argv': [sys.executable, '-c',
                'print("abcdef1234567890 healthy")']}, native.ws)
        delivery = {'publish_ref': publish['receipt'], 'verification_ref': verify['receipt'],
                    'expected_sha': 'abcdef1234567890', 'live_sha': 'abcdef1234567890',
                    'health': 'healthy', 'source_paths': ['answer.txt']}
        self.assertTrue(native.request('candidate', {**common, 'delivery_evidence': delivery})['ok'])
        self.assertEqual(native.state()['status'], 'reviewing')

    def test_f8_lost_ack_replay_keeps_one_contract_transition(self):
        from adhd.native import submit_request, bridge
        native = self.native
        request_id = 'a' * 32
        payload = {'mode': 'coding', 'criteria': [{'id': 'R1', 'text': 'Result', 'kind': 'artifact'}],
                   'artifacts': ['answer.txt'], 'execution_profile': 'simple'}
        queued = submit_request(native.key, native.ws, 'begin', payload, request_id)
        native.event('PostToolUse', tool_name='Bash')
        first = json.loads(Path(queued['receipt']).read_text(encoding='utf-8'))
        self.assertTrue(first['ok'])
        run_id = native.state()['run_id']
        original = bridge(native.ws, native.key) / 'processed' / (request_id + '.json')
        duplicate = bridge(native.ws, native.key) / 'inbox' / (request_id + '.json')
        duplicate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, duplicate)
        Path(queued['receipt']).unlink()
        native.event('PostToolUse', tool_name='Bash')
        self.assertEqual(json.loads(Path(queued['receipt']).read_text(encoding='utf-8')), first)
        self.assertEqual(native.state()['run_id'], run_id)

    def test_f1_host_denial_blocks_matching_retry_in_native_hook(self):
        native = self.native
        native.begin()
        native.event('UserPromptSubmit', prompt='demo 게시를 승인해')
        turn = native.state()['prompts'][-1]['turn_id']
        request = {'target': 'demo'}
        action = {'operation': 'publish', 'canonical_targets': ['demo'],
                  'scope_digest': digest('demo'), 'source_turn_id': turn,
                  'authorization_excerpt': 'demo 게시를 승인해', 'constraints': [],
                  'tool_name': 'mcp__host__publish', 'request_sha256': digest(request)}
        self.assertTrue(native.request('record-action', action)['ok'])
        from adhd.execution_decisions import action_key
        self.assertTrue(native.request('authorize-action',
            {'action_id': action_key(action), 'source_turn_id': turn})['ok'])
        native.event('PostToolUse', tool_name='mcp__host__publish', tool_use_id='host-1',
                     tool_input=request, tool_response={'isError': True,
                         'code': 'policy_denied', 'message': 'blocked by policy'}, policy_version='v1')
        outcome = native.event('PreToolUse', tool_name='mcp__host__publish', tool_use_id='host-2',
                               tool_input=request, policy_version='v1')
        self.assertEqual(outcome['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertTrue(native.state()['actions'][action_key(action)]['authorized'])

    def test_f1_failed_os_permission_receipt_is_not_host_policy(self):
        from adhd.evidence import run_check
        from adhd.execution_decisions import action_key
        native = self.native
        native.begin()
        native.event('UserPromptSubmit', prompt='demo 게시를 승인해')
        turn = native.state()['prompts'][-1]['turn_id']
        action = {'operation': 'publish', 'canonical_targets': ['demo'],
                  'scope_digest': digest('demo'), 'source_turn_id': turn,
                  'authorization_excerpt': 'demo 게시를 승인해', 'constraints': []}
        self.assertTrue(native.request('record-action', action)['ok'])
        self.assertTrue(native.request('authorize-action',
            {'action_id': action_key(action), 'source_turn_id': turn})['ok'])
        (native.ws / 'answer.txt').write_text('source', encoding='utf-8')
        state = native.state()
        failed = run_check({'run_id': state['run_id'],
            'contract_revision': state['intent_version'], 'subject_paths': ['answer.txt'],
            'argv': [sys.executable, '-c',
                     'raise PermissionError(13, "Permission denied", "C:/fixture")']}, native.ws)
        self.assertNotEqual(failed['exit_code'], 0)
        self.assertTrue(native.request('action-outcome', {'action_id': action_key(action),
            'outcome': 'os_permission_denied', 'evidence_id': failed['receipt']})['ok'])
        self.assertEqual(native.state()['actions'][action_key(action)]['attempts'][0]['outcome'],
                         'os_permission_denied')


if __name__ == '__main__':
    unittest.main()
