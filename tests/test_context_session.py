import json
from pathlib import Path
import tempfile
import unittest

from adhd.context_session import ContextSession, handoff, status


class ContextSessionTests(unittest.TestCase):
    def test_dedup_resets_after_compaction_or_new_agent_and_budget_is_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = {'source': 'code', 'path': 'a.py', 'revision': 'abc', 'policy_version': 'v1', 'range': [1, 2]}
            with ContextSession(root, 'test', root=root / 'sidecars') as session:
                one = session.deliver(ref, '한국어' * 100, max_chars=100, max_bytes=30)
                self.assertLessEqual(one['bytes'], 30)
                self.assertTrue(one['truncated'])
                self.assertTrue(session.deliver(ref, '한국어' * 100, max_chars=100, max_bytes=30)['duplicate'])
                self.assertFalse(session.deliver(ref, '한국어' * 100, actor='child', max_chars=100, max_bytes=30)['duplicate'])
                self.assertFalse(session.deliver(ref, '한국어' * 100, epoch='after-compact', max_chars=100, max_bytes=30)['duplicate'])
                self.assertEqual(30, session.trace()['avoided_repeat_bytes'])

    def test_changed_revision_is_not_deduplicated_and_secrets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with ContextSession(root, 'test', root=root / 'sidecars') as session:
                ref = {'source': 'code', 'revision': 'a', 'policy_version': 'v1'}
                session.deliver(ref, 'text')
                self.assertFalse(session.deliver({**ref, 'revision': 'b'}, 'text')['duplicate'])
                with self.assertRaises(ValueError):
                    session.deliver(ref, 'sk-' + 'a' * 32)

    def test_handoff_keeps_original_intent_and_marks_idle_contract_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, key = Path(tmp), 'a' * 24
            view_path = root / '.adhd' / 'bridge' / key / 'view.json'
            view_path.parent.mkdir(parents=True)
            original = '$adhd-goal preserve the original graph and implement P0-P6'
            view_path.write_text(json.dumps({'workspace': str(root), 'key': key, 'status': 'idle',
                'prompts': [{'text': original, 'turn_id': 't'}], 'goal_request': {'outcome': original}}))
            packet = handoff(root, key)
            self.assertEqual(original, packet['original_requests'][0]['text'])
            self.assertIn('native_contract_not_established', packet['missing'])
            result = status(root, key, write=True)
            self.assertIn(original, result['markdown'])
            self.assertTrue((root / '.adhd/context-status.md').is_file())
