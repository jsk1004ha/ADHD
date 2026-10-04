"""Regression coverage for native large-task and deep-profile release gates."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from adhd.core import digest, file_hash, atomic_json
from adhd.native import folder, progress_markers
from tests import test_native


class NativeLargeReleaseTests(unittest.TestCase):
    def test_deep_large_candidate_progress_and_approval_order(self):
        fixture = test_native.NativeTests(methodName='test_idle_does_not_loop')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        plan = {
            'objective': 'Deliver verified output', 'approach': 'Implement and check',
            'verification': 'Run the result check', 'preflight': ['Inspect input'],
            'risks': ['Regression'], 'alternatives': ['Small patch'],
            'steps': [{'id': 'S1', 'action': 'Implement and check',
                       'requirements': ['R1'], 'depends_on': []}],
        }
        begin = fixture.request('begin', {
            'mode': 'coding', 'execution_profile': 'deep',
            'criteria': [{'id': 'R1', 'text': 'Behavior passes', 'kind': 'test'}],
            'artifacts': ['answer.txt'], 'plan': plan,
        })
        self.assertTrue(begin['ok'], begin)
        state = fixture.state()
        state['large_task'] = {'run_id': state['run_id']}
        fixture.save(state)

        stage = fixture.ws / 'stage'
        stage.mkdir()
        part = stage / 'part.txt'
        part.write_text('assembled part', encoding='utf-8')
        frozen = {'staging_workspace': str(stage), 'files': {'part.txt': file_hash(part)}}
        frozen['snapshot_digest'] = digest(frozen)
        atomic_json(fixture.ws / 'frozen.json', frozen)
        proof = {'snapshot_manifest': 'frozen.json', 'snapshot_digest': frozen['snapshot_digest']}
        accepted = []

        def accept_before_completion(review_state):
            self.assertEqual(review_state['review_receipt']['verdict']['verdict'], 'approve')
            self.assertFalse((folder(fixture.key) / ('completion-' + review_state['run_id'] + '.json')).exists())
            accepted.append(review_state['candidate']['digest'])

        with (patch('adhd.large_native.candidate_evidence', return_value=(proof, [])),
              patch('adhd.large_native.validate_admission'),
              patch('adhd.large_native.accept_verified', side_effect=accept_before_completion)):
            candidate_ack = fixture.candidate(large_task_evidence=proof)
            self.assertTrue(candidate_ack['ok'], candidate_ack)
            candidate = fixture.state()['candidate']
            self.assertEqual(candidate['large_task_evidence'], proof)
            self.assertGreaterEqual(candidate['recorded_at'], state['started'])
            self.assertTrue(any(marker.startswith('requirement_pass:')
                                for marker in progress_markers(fixture.state())))
            fixture.spawn(role='adhd-scout', aid='reader')
            fixture.spawn()
            fixture.review(evidence_review={
                'target_covering_receipts': candidate['deep_evidence']['target_covering_receipts'],
                'findings': ['Checked the current execution against the declared output.'],
            })
            pending = fixture.state()
            self.assertEqual(pending['status'], 'revising')
            self.assertFalse((folder(fixture.key) / ('completion-' + pending['run_id'] + '.json')).exists())
            self.assertEqual(accepted, [])
            fixture.event('SubagentStop', agent_type='adhd-scout', agent_id='reader',
                          last_assistant_message='Read complete')
            fixture.spawn(aid='second-review')
            fixture.review(aid='second-review', evidence_review={
                'target_covering_receipts': candidate['deep_evidence']['target_covering_receipts'],
                'findings': ['Checked the current execution against the declared output.'],
            })

        final = fixture.state()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(accepted, [candidate['digest']])
        self.assertTrue((folder(fixture.key) / ('completion-' + final['run_id'] + '.json')).is_file())
