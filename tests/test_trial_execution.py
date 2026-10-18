import json
from pathlib import Path
import sys
import tempfile
import unittest
import time
from unittest.mock import patch

from adhd.evidence import run_check, validate_execution, _store_observed_output
from adhd.trial_registry import TrialRegistry, CONDITIONS


class TrialExecutionTests(unittest.TestCase):
    def test_cli_time_flags_are_explicit_begin_controls(self):
        from adhd.cli import main
        with patch('adhd.cli.submit_request', return_value={'queued': 'transport-only'}) as submit, \
                patch('adhd.cli.output'):
            self.assertEqual(main(['native', 'begin', '--session', 'example',
                                   '--time-limit-seconds', '900', '--review-reserve-seconds', '180']), 0)
            sent = submit.call_args.args[3]
            self.assertEqual(sent['time_limit_seconds'], 900)
            self.assertEqual(sent['review_reserve_seconds'], 180)
            self.assertEqual(main(['native', 'sync-intent', '--session', 'example',
                '--classification', 'new_task', '--source-turn-id', 'recorded-turn', '--base-revision', '3']), 0)
            self.assertEqual(submit.call_args.args[3], {'classification': 'new_task',
                'source_turn_id': 'recorded-turn', 'base_revision': 3, 'operations': []})
        for operation, flags in [('checkpoint', ['--time-limit-seconds', '900']),
                                 ('begin', ['--review-reserve-seconds', '60']),
                                 ('begin', ['--time-limit-seconds', '60', '--review-reserve-seconds', '60']),
                                 ('begin', ['--time-limit-seconds', 'nan']),
                                 ('sync-intent', ['--classification', 'new_task']),
                                 ('begin', ['--classification', 'new_task', '--source-turn-id', 't', '--base-revision', '3'])]:
            with self.subTest(operation=operation, flags=flags), \
                    patch('adhd.cli.submit_request') as submit, patch('sys.stderr'):
                self.assertEqual(main(['native', operation, '--session', 'example', *flags]), 2)
                submit.assert_not_called()

    def test_cli_budget_exhaustion_is_not_a_success_exit(self):
        from adhd.cli import main
        with patch('adhd.large_cli.execute', return_value={'status': 'budget_exhausted'}), \
                patch('adhd.cli.output'):
            self.assertEqual(main(['batch', 'run']), 2)

    def test_expired_shared_deadline_does_not_create_an_observed_process_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            (ws / 'input.txt').write_text('fixed')
            spec = {'argv': [sys.executable, '-c', 'print("should not run")'],
                    'subject_paths': ['input.txt'], 'run_id': 'budget', 'contract_revision': 1}
            with patch('adhd.evidence.execute_native_check') as process:
                with self.assertRaises(RuntimeError):
                    run_check(spec, ws, deadline_monotonic=time.monotonic() - 1)
                process.assert_not_called()
            self.assertFalse(list(ws.glob('.adhd/checks/*/receipt.json')))

    def test_deadline_expiring_during_input_hashing_still_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp)
            (ws / 'input.txt').write_text('fixed')
            spec = {'argv': [sys.executable, '-c', 'pass'], 'subject_paths': ['input.txt'],
                    'run_id': 'budget', 'contract_revision': 1}
            with patch('adhd.evidence.time.monotonic', side_effect=[1, 3]), \
                    patch('adhd.evidence.execute_native_check') as process:
                with self.assertRaises(RuntimeError):
                    run_check(spec, ws, deadline_monotonic=2)
                process.assert_not_called()

    def test_real_success_and_failure_keep_receipts_and_all_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            ws=Path(temp)
            script=ws/'check.py'
            script.write_text('import sys\nprint("observed")\nsys.exit(int(sys.argv[1]))\n')
            metadata={'case_id':'check','task_type':'windows_code','variant':'candidate',
                      'conditions':{key:'fixed' for key in CONDITIONS}}
            for code in (0,2):
                spec={'argv':[sys.executable,str(script),str(code)],'subject_paths':['check.py'],
                      'run_id':'test-run','contract_revision':1,'trial':dict(metadata,case_id='check-'+str(code))}
                result=run_check(spec,ws)
                receipt=validate_execution(ws,result['receipt'],run_id='test-run',revision=1,expect_failure=bool(code))
                self.assertEqual(receipt['result']['exit_code'],code)
                self.assertEqual(receipt['schema_version'],2)
            registry=TrialRegistry(ws/'.adhd/trials-v1')
            summary=registry.summary()
            self.assertEqual(summary['started'],2)
            self.assertEqual(summary['statuses'],{'success':1,'failed':1})
            rows=list(registry.trials())
            self.assertTrue(all(row['resources']['sampler_stopped'] for row in rows))
            for row in rows:
                post_task = row['resources']['post_task']
                self.assertTrue(post_task['sampler_stopped'])
                self.assertGreater(post_task['window_elapsed_ms'], 0)
                self.assertIn('remaining_count', post_task)
                self.assertIn('remaining_cpu_delta_seconds', post_task)
                self.assertNotIn('whole_host_idle_cpu', post_task)
            self.assertTrue(all(row['usage']['coverage']!='complete' for row in rows))

    def test_observed_output_reference_is_full_and_secret_capture_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            ws=Path(temp)
            state={'workspace':str(ws)}
            output={'text':'상세 근거\n'*2000,'truncated':False,'exit_code':0}
            self.assertEqual(_store_observed_output(state,'a'*64,output)['reason'],'capture_disabled')
            (ws/'.adhd').mkdir()
            (ws/'.adhd/tool-output-policy.json').write_text(json.dumps({'schema_version':1,'enabled':True,
                'allowed_tools':['mcp__test__run'],'policy_revision':'reviewed-v1'}))
            result=_store_observed_output(state,'a'*64,output,tool_name='mcp__test__run')
            self.assertEqual(result['status'],'stored')
            self.assertEqual(json.loads((ws/result['path']).read_text(encoding='utf-8')),output)
            self.assertFalse(result['reported_truncated'])
            blocked=_store_observed_output(state,'b'*64,{'authorization':'hidden'},tool_name='mcp__test__run')
            self.assertEqual(blocked['status'],'uncollected')
            self.assertFalse((ws/'.adhd/tool-results'/('b'*64+'.json')).exists())


if __name__=='__main__':unittest.main()
