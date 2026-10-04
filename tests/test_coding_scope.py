from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd.coding_scope import audit, capture, scope_repositories, validate_report
from adhd.core import ROOT, atomic_json, read_json
from adhd.evidence import run_check
from adhd.native import folder, handle_event, is_fresh, session_key, submit_request


@unittest.skipUnless(shutil.which('git'), 'Git is required for real scope fixtures')
class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.root.mkdir()
        self.git('init', '-q')
        (self.root / '.gitignore').write_text('.adhd/\n__pycache__/\n', encoding='utf-8')
        (self.root / 'a.py').write_text('original\n', encoding='utf-8')
        (self.root / 'outside.txt').write_text('preserved\n', encoding='utf-8')
        self.git('add', '.')
        self.commit()

    def git(self, *args, root=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        return subprocess.check_output(['git', '--no-optional-locks', '-C', str(root or self.root),
                                        *args], stderr=subprocess.PIPE, env=env)

    def commit(self):
        self.git('-c', 'commit.gpgsign=false', '-c', 'user.name=Scope fixture',
                 '-c', 'user.email=scope@example.invalid', 'commit', '-qm', 'fixture')


class CodingScopeTests(GitFixture):
    def test_repository_discovery_includes_checkout_above_workspace(self):
        nested = self.root / 'nested'
        nested.mkdir()
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            self.assertEqual(scope_repositories(nested, ['new.py']), {self.root})

    def test_scope_audit_preserves_existing_staged_unstaged_and_untracked_work(self):
        (self.root / 'outside.txt').write_text('staged\n', encoding='utf-8')
        self.git('add', 'outside.txt')
        (self.root / 'outside.txt').write_text('unstaged\n', encoding='utf-8')
        (self.root / 'user.txt').write_text('untracked\n', encoding='utf-8')
        before = self.git('status', '--porcelain=v1', '-z')
        baseline = capture(self.root, '.', ['a.py'])
        (self.root / 'a.py').write_text('requested\n', encoding='utf-8')
        report = audit(self.root, baseline)
        self.assertEqual(report['changes'], ['a.py'])
        self.assertEqual((self.root / 'user.txt').read_text(), 'untracked\n')
        self.assertEqual(before, self.git('status', '--porcelain=v1', '-z').replace(b' M a.py\0', b''))

    def test_preexisting_worktree_change_is_rejected(self):
        (self.root / 'outside.txt').write_text('user work\n', encoding='utf-8')
        baseline = capture(self.root, '.', ['a.py', 'outside.txt'])
        (self.root / 'outside.txt').write_text('overwritten\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Pre-existing work changed'):
            audit(self.root, baseline)

    def test_index_only_change_to_existing_work_is_rejected(self):
        (self.root / 'outside.txt').write_text('user work\n', encoding='utf-8')
        self.git('add', 'outside.txt')
        baseline = capture(self.root, '.', ['outside.txt'])
        (self.root / 'outside.txt').write_text('different index\n', encoding='utf-8')
        self.git('add', 'outside.txt')
        (self.root / 'outside.txt').write_text('user work\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Pre-existing work changed'):
            audit(self.root, baseline)

    def test_scope_cannot_be_escaped_with_untracked_file_or_directory_prefix(self):
        baseline = capture(self.root, '.', ['src/'])
        (self.root / 'src2').mkdir()
        (self.root / 'src2' / 'unrelated.py').write_text('extra\n')
        with self.assertRaisesRegex(ValueError, 'outside coding scope'):
            audit(self.root, baseline)

    def test_outside_staged_change_is_rejected_when_worktree_matches_baseline(self):
        baseline = capture(self.root, '.', ['a.py'])
        (self.root / 'outside.txt').write_text('staged change\n', encoding='utf-8')
        self.git('add', 'outside.txt')
        (self.root / 'outside.txt').write_text('preserved\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outside coding scope'):
            audit(self.root, baseline)

    def test_preexisting_index_only_work_is_captured_and_preserved(self):
        (self.root / 'outside.txt').write_text('staged user work\n', encoding='utf-8')
        self.git('add', 'outside.txt')
        (self.root / 'outside.txt').write_text('preserved\n', encoding='utf-8')
        baseline = capture(self.root, '.', ['a.py'])
        self.assertEqual(list(baseline['dirty_files']), ['outside.txt'])
        self.assertEqual(audit(self.root, baseline)['changes'], [])
        self.git('add', 'outside.txt')
        with self.assertRaisesRegex(ValueError, 'Pre-existing work changed'):
            audit(self.root, baseline)

    def test_deletion_and_rename_are_both_checked(self):
        baseline = capture(self.root, '.', ['renamed.py'])
        (self.root / 'a.py').rename(self.root / 'renamed.py')
        with self.assertRaisesRegex(ValueError, 'a.py'):
            audit(self.root, baseline)

    def test_changes_remain_visible_after_commit(self):
        baseline = capture(self.root, '.', ['a.py'])
        (self.root / 'a.py').write_text('requested\n')
        self.git('add', 'a.py')
        self.commit()
        self.assertEqual(audit(self.root, baseline)['changes'], ['a.py'])

    def test_commit_cannot_include_preexisting_staged_work(self):
        (self.root / 'outside.txt').write_text('staged user work\n', encoding='utf-8')
        self.git('add', 'outside.txt')
        baseline = capture(self.root, '.', ['a.py'])
        (self.root / 'a.py').write_text('requested\n', encoding='utf-8')
        self.git('add', 'a.py')
        # Committing only the assigned path leaves the user's staged change intact.
        self.git('-c', 'commit.gpgsign=false', '-c', 'user.name=Scope fixture',
                 '-c', 'user.email=scope@example.invalid', 'commit', '-qm', 'requested', '--only', '--', 'a.py')
        self.assertEqual(audit(self.root, baseline)['changes'], ['a.py'])
        # A later commit of that same unchanged index must still be rejected.
        self.commit()
        with self.assertRaisesRegex(ValueError, 'Pre-existing work committed: outside.txt'):
            audit(self.root, baseline)

    def test_unicode_paths_and_empty_new_files_are_bound(self):
        baseline = capture(self.root, '.', ['한글 파일.py'])
        (self.root / '한글 파일.py').write_bytes(b'')
        report = audit(self.root, baseline)
        self.assertEqual(report['changes'], ['한글 파일.py'])
        validate_report(self.root, baseline, report)
        (self.root / '한글 파일.py').write_bytes(b'new')
        with self.assertRaisesRegex(ValueError, 'source content changed'):
            validate_report(self.root, baseline, report)

    def test_unsafe_scope_paths_and_missing_base_ref_are_rejected(self):
        for path in ['../outside', 'C:/private', '.git/', 'src/*', './a.py']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                capture(self.root, '.', [path])
        baseline = capture(self.root, '.', ['a.py'])
        baseline['base_ref'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'Git scope audit failed'):
            audit(self.root, baseline)

    def test_passive_freshness_detects_new_files_and_index_changes_without_git(self):
        baseline = capture(self.root, '.', ['a.py'])
        (self.root / 'a.py').write_text('requested\n')
        report = audit(self.root, baseline)
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            validate_report(self.root, baseline, report)
            (self.root / 'new.txt').write_text('late\n')
            with self.assertRaisesRegex(ValueError, 'inventory changed'):
                validate_report(self.root, baseline, report)
        (self.root / 'new.txt').unlink()
        self.git('add', 'a.py')
        with self.assertRaisesRegex(ValueError, 'Git metadata changed'):
            validate_report(self.root, baseline, report)

    def test_scope_cannot_follow_source_symlinks(self):
        baseline = capture(self.root, '.', ['link.py'])
        try:
            (self.root / 'link.py').symlink_to(self.root / 'a.py')
        except OSError:
            self.skipTest('This Windows account cannot create symlinks')
        with self.assertRaisesRegex(ValueError, 'link or junction'):
            audit(self.root, baseline)


class NativeCodingScopeTests(GitFixture):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'CODEX_HOME': str(Path(self.temp.name).resolve() / 'codex'),
                                     'ADHD_EXEC_OWNER': ''})
        env.start()
        self.addCleanup(env.stop)
        self.sid = uuid.uuid4().hex
        self.key = session_key(self.sid)
        self.turn = 0
        self.event('UserPromptSubmit', prompt='Update a.py only, preserve existing work')
        self.baseline_rel = '.adhd/scope-baseline.json'
        atomic_json(self.root / self.baseline_rel, capture(self.root, '.', ['a.py']))

    def event(self, event, **extra):
        self.turn += 1
        return handle_event({'hook_event_name': event, 'session_id': self.sid,
                             'cwd': str(self.root), 'turn_id': str(self.turn), **extra})

    def state(self):
        return read_json(folder(self.key) / 'state.json')

    def request(self, operation, payload):
        queued = submit_request(self.key, self.root, operation, payload)
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            self.event('PostToolUse', tool_name='Bash')
        return read_json(Path(queued['receipt']))

    def begin(self, *, scoped=True):
        payload = {'mode': 'coding', 'criteria': [{'id': 'R1', 'kind': 'behavior', 'text': 'Update a.py'}],
                   'artifacts': ['a.py'], 'plan': {'objective': 'Update a.py', 'approach': 'Minimal edit',
                   'verification': 'Run scope audit', 'preflight': ['Inspect source'], 'risks': ['Scope escape'],
                   'alternatives': ['No-op if already correct'], 'steps': [{'id': 'S1', 'action': 'Edit a.py',
                   'requirements': ['R1'], 'depends_on': []}]}}
        if scoped:
            payload['coding_scope'] = self.baseline_rel
        return self.request('begin', payload)

    def evidence(self, *, wrong_command=False):
        (self.root / 'a.py').write_text('requested\n', encoding='utf-8')
        report_rel = '.adhd/scope-report.json'
        atomic_json(self.root / report_rel, audit(self.root, read_json(self.root / self.baseline_rel)))
        argv = [str(Path(sys.executable).resolve()), str(ROOT / 'adhd.py'), 'coding-scope', 'verify',
                '--workspace', str(self.root), '--baseline', self.baseline_rel, '--report', report_rel]
        if wrong_command:
            argv = [sys.executable, '-c', 'pass']
        check = run_check({'run_id': self.state()['run_id'], 'contract_revision': self.state()['intent_version'],
                          'argv': argv, 'subject_paths': [self.baseline_rel, report_rel, 'a.py']}, self.root)
        self.assertEqual(check['exit_code'], 0)
        return {'report': report_rel, 'receipt': check['receipt'], 'change_coverage': [
            {'path': 'a.py', 'requirements': ['R1'], 'reason': 'Requested source update'}]}

    def candidate(self, evidence):
        return self.request('candidate', {'files': ['a.py'], 'criterion_results': [
            {'id': 'R1', 'pass': True, 'evidence': 'Requested source updated and scope checked'}],
            'coding_scope_evidence': evidence})

    def test_git_task_requires_baseline_before_begin(self):
        result = self.begin(scoped=False)
        self.assertFalse(result['ok'])
        self.assertIn('coding_scope baseline', result['message'])

    def test_workspace_inside_checkout_cannot_bypass_scope(self):
        nested = self.root / 'nested'
        nested.mkdir()
        sid = uuid.uuid4().hex
        handle_event({'hook_event_name': 'UserPromptSubmit', 'session_id': sid,
                      'cwd': str(nested), 'turn_id': '1', 'prompt': 'Update new.py only'})
        payload = {'mode': 'coding', 'criteria': [{'id': 'R1', 'kind': 'behavior', 'text': 'Update new.py'}],
                   'artifacts': ['new.py'], 'plan': {'objective': 'Update new.py', 'approach': 'Minimal edit',
                   'verification': 'Run scope audit', 'preflight': ['Inspect source'], 'risks': ['Scope escape'],
                   'alternatives': ['No-op if already correct'], 'steps': [{'id': 'S1', 'action': 'Edit new.py',
                   'requirements': ['R1'], 'depends_on': []}]}}
        queued = submit_request(session_key(sid), nested, 'begin', payload)
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            handle_event({'hook_event_name': 'PostToolUse', 'session_id': sid,
                          'cwd': str(nested), 'turn_id': '2', 'tool_name': 'Bash'})
        result = read_json(Path(queued['receipt']))
        self.assertFalse(result['ok'])
        self.assertIn('workspace must include the checkout root', result['message'])

    def test_baseline_for_another_repository_cannot_cover_declared_source(self):
        other = self.root / 'other'
        other.mkdir()
        self.git('init', '-q', root=other)
        (other / 'b.py').write_text('other\n')
        self.git('add', 'b.py', root=other)
        self.git('-c', 'commit.gpgsign=false', '-c', 'user.name=Scope fixture',
                 '-c', 'user.email=scope@example.invalid', 'commit', '-qm', 'other', root=other)
        atomic_json(self.root / self.baseline_rel, capture(self.root, 'other', ['b.py']))
        result = self.begin()
        self.assertFalse(result['ok'])
        self.assertIn('scoped repository', result['message'])

    def test_candidate_requires_scope_evidence_and_requirement_coverage(self):
        self.assertTrue(self.begin()['ok'])
        self.assertFalse(self.candidate(None)['ok'])
        evidence = self.evidence()
        evidence['change_coverage'] = []
        self.assertFalse(self.candidate(evidence)['ok'])
        evidence['change_coverage'] = [{'path': 'a.py', 'requirements': ['unknown'], 'reason': 'Extra'}]
        self.assertFalse(self.candidate(evidence)['ok'])

    def test_unrelated_success_receipt_cannot_replace_scope_verification(self):
        self.assertTrue(self.begin()['ok'])
        result = self.candidate(self.evidence(wrong_command=True))
        self.assertFalse(result['ok'])
        self.assertIn('pinned coding-scope verify', result['message'])

    def test_late_unlisted_file_invalidates_candidate(self):
        self.assertTrue(self.begin()['ok'])
        self.assertTrue(self.candidate(self.evidence())['ok'])
        with patch('adhd.coding_scope._git', side_effect=AssertionError('hook ran Git')):
            self.assertTrue(is_fresh(self.state()))
            (self.root / 'late.py').write_text('unexpected\n', encoding='utf-8')
            self.assertFalse(is_fresh(self.state()))

    def test_baseline_cannot_be_rewritten_after_begin(self):
        self.assertTrue(self.begin()['ok'])
        evidence = self.evidence()
        baseline = read_json(self.root / self.baseline_rel)
        baseline['allowed_paths'].append('outside.txt')
        atomic_json(self.root / self.baseline_rel, baseline)
        result = self.candidate(evidence)
        self.assertFalse(result['ok'])
        self.assertIn('baseline changed', result['message'])


if __name__ == '__main__':
    unittest.main()
