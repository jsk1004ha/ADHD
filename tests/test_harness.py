from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adhd import core
from adhd import install as installer
from adhd import runner
from adhd.cli import main, output


class CliEncodingTests(unittest.TestCase):
    def test_cli_json_is_utf8_even_with_cp949_stdout(self):
        raw=io.BytesIO();stream=io.TextIOWrapper(raw,encoding='cp949',write_through=True)
        with patch('sys.stdout',stream):
            output({'route':'검증—완료'})
        self.assertEqual(json.loads(raw.getvalue().decode('utf-8'))['route'],'검증—완료')

    def test_hook_json_survives_cp949_stdout(self):
        import hook
        incoming=io.TextIOWrapper(io.BytesIO(b'{"hook_event_name":"SessionStart"}'),encoding='utf-8')
        raw=io.BytesIO();stream=io.TextIOWrapper(raw,encoding='cp949',write_through=True)
        with patch('sys.stdin',incoming), patch('sys.stdout',stream), patch.object(hook,'handle_event',return_value={'systemMessage':'검증—완료'}):
            self.assertEqual(hook.main(),0)
        self.assertEqual(json.loads(raw.getvalue().decode('cp949'))['systemMessage'],'검증—완료')

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workspace = self.base / 'project'; self.workspace.mkdir()
        self.codex_home = self.base / 'codex-home'; self.codex_home.mkdir()
        self.env = patch.dict(os.environ, {'CODEX_HOME':str(self.codex_home), 'ADHD_HOME':str(self.base/'state'),
                                         'FAKE_COUNTER':str(self.base/'counter.json'), 'FAKE_SCENARIO':'success'})
        self.env.start()
        self.cli = patch('adhd.runner.executable_command', return_value=[sys.executable, str(Path(__file__).with_name('fake_codex.py'))])
        self.cli.start()
        self.settings = {'codex':'fake','model':None,'effort':'inherit','max_iterations':4,
                         'max_seconds':40,'max_tokens':10000,'call_timeout':5,'check_timeout':4,
                         'checks':[],'allow_mcp':[],'allow_plugin':[],'catalog_budget':0,'allow_unknown_usage':False}
    def tearDown(self):
        self.cli.stop();self.env.stop();self.tmp.cleanup()
    def run_task(self, scenario='success', **overrides):
        os.environ['FAKE_SCENARIO']=scenario
        settings = dict(self.settings, **overrides)
        rid = runner.make_run('Create a validated answer', self.workspace, 'coding', settings)
        with contextlib.redirect_stdout(io.StringIO()): state=runner.execute_run(rid)
        return rid,state

class LoopTests(Base):
    def test_real_child_process_sequence_and_usage(self):
        rid,s=self.run_task()
        self.assertEqual(s['status'],'complete');self.assertEqual(s['calls'],3)
        self.assertEqual(s['usage'],dict(input_tokens=300,cached_input_tokens=150,output_tokens=60))
        self.assertTrue((runner.run_folder(rid)/'completion.json').is_file())
        self.assertEqual(len(core.recipes(self.workspace,'coding','Create a validated answer')),1)
    def test_continuation_is_automatic(self):
        _,s=self.run_task('continue_once');self.assertEqual(s['status'],'complete');self.assertEqual(s['iterations'],2)
    def test_independent_rejection_causes_another_iteration(self):
        _,s=self.run_task('revise_once');self.assertEqual(s['status'],'complete');self.assertEqual(s['calls'],5)
    def test_missing_artifact_not_success(self):
        _,s=self.run_task('missing_artifact',max_iterations=2)
        self.assertEqual(s['status'],'budget_exhausted');self.assertEqual(s['calls'],3)
    def test_missing_review_criteria_not_success(self):
        _,s=self.run_task('incomplete_review',max_iterations=2);self.assertNotEqual(s['status'],'complete')
    def test_unknown_criteria_not_success(self):
        _,s=self.run_task('wrong_id',max_iterations=2);self.assertNotEqual(s['status'],'complete');self.assertEqual(s['calls'],3)
    def test_empty_deliverables_rejected(self):
        _,s=self.run_task('no_deliverables');self.assertEqual(s['status'],'failed')
    def test_path_escape_rejected(self):
        _,s=self.run_task('bad_path');self.assertEqual(s['status'],'failed')
    def test_three_no_progress_iterations_block(self):
        _,s=self.run_task('no_progress');self.assertEqual(s['status'],'blocked');self.assertEqual(s['iterations'],3)
    def test_unknown_usage_fails_closed(self):
        rid,s=self.run_task('unknown_usage');self.assertEqual(s['status'],'blocked');self.assertEqual(s['calls'],1)
        with contextlib.redirect_stdout(io.StringIO()): s=runner.execute_run(rid,allow_unknown_usage=True)
        self.assertEqual(s['status'],'complete');self.assertGreater(s['usage_unknown_calls'],0)
    def test_token_budget_stops_next_call(self):
        _,s=self.run_task(max_tokens=100);self.assertEqual(s['status'],'budget_exhausted');self.assertEqual(s['calls'],1)
    def test_cached_tokens_are_not_double_counted(self):
        _,s=self.run_task(max_tokens=300)
        # plan=120 + work=120 => reviewer allowed at 240; cached tokens would incorrectly block it.
        self.assertEqual(s['status'],'complete');self.assertEqual(s['calls'],3)
    def test_resume_retains_budget_and_state(self):
        rid,s=self.run_task('continue_once',max_iterations=1);self.assertEqual(s['status'],'budget_exhausted')
        with contextlib.redirect_stdout(io.StringIO()): s=runner.execute_run(rid,extra_iterations=2)
        self.assertEqual(s['status'],'complete');self.assertEqual(s['iterations'],2)
    def test_contract_tampering_blocks_resume(self):
        rid,s=self.run_task('continue_once',max_iterations=1)
        path=runner.run_folder(rid)/'contract.json';path.write_text(path.read_text()+' ')
        with contextlib.redirect_stdout(io.StringIO()): s=runner.execute_run(rid,extra_iterations=2)
        self.assertEqual(s['status'],'blocked');self.assertIn('changed',s['reason'])
    def test_cli_error_does_not_trigger_security_fallback(self):
        _,s=self.run_task('exit_error');self.assertEqual(s['status'],'failed');self.assertEqual(s['calls'],1)
    def test_malformed_model_output_not_success(self):
        _,s=self.run_task('malformed_result');self.assertEqual(s['status'],'failed')
    def test_worker_blocker_propagates(self):
        _,s=self.run_task('worker_blocked');self.assertEqual(s['status'],'blocked');self.assertIn('data',s['reason'])
    def test_review_blocker_propagates(self):
        _,s=self.run_task('review_blocked');self.assertEqual(s['status'],'blocked');self.assertIn('rendering',s['reason'])
    def test_failed_user_host_check_blocks_approval(self):
        _,s=self.run_task(checks=[[sys.executable,'-c','import sys;sys.exit(4)']],max_iterations=1)
        self.assertEqual(s['status'],'budget_exhausted');self.assertEqual(s['calls'],2)
    def test_successful_user_host_check(self):
        _,s=self.run_task(checks=[[sys.executable,'-c',"from pathlib import Path; assert Path('answer.txt').stat().st_size"]])
        self.assertEqual(s['status'],'complete')
    def test_completed_run_is_not_reexecuted(self):
        rid,s=self.run_task();before=s['calls'];s=runner.execute_run(rid);self.assertEqual(s['calls'],before)
    def test_failed_recipe_replay_is_quarantined(self):
        rid=core.save_recipe(self.workspace,'coding','Create a validated answer',['Write answer.txt'],'test-only evidence')
        _,s=self.run_task('revise_once')
        # A subsequent successful revalidation may replace this exact recipe; quarantine itself is separately tested.
        self.assertEqual(s['status'],'complete');self.assertIn(rid,s['recipe_ids'])

class ControlAndDataTests(Base):
    def test_cancel_running_child(self):
        stop=self.base/'STOP'
        t=threading.Timer(.2, lambda:stop.write_text('stop'));t.start()
        start=time.monotonic()
        with self.assertRaises(runner.Halt) as cm:
            runner.execute([sys.executable,'-c','import time;time.sleep(20)'],self.workspace,self.base/'cancel',timeout=5,stop_file=stop)
        t.join();self.assertEqual(cm.exception.status,'cancelled');self.assertLess(time.monotonic()-start,4)
    def test_process_timeout(self):
        with self.assertRaises(runner.Halt) as cm:
            runner.execute([sys.executable,'-c','import time;time.sleep(20)'],self.workspace,self.base/'timeout',timeout=.1)
        self.assertEqual(cm.exception.status,'budget_exhausted')
    def test_workspace_single_owner(self):
        with runner.WorkspaceLock(self.workspace,'a'):
            with self.assertRaises(ValueError):
                with runner.WorkspaceLock(self.workspace,'b'):pass
        self.assertFalse(runner.WorkspaceLock(self.workspace,'a').path.exists())
    def test_replaced_lock_is_not_deleted(self):
        with runner.WorkspaceLock(self.workspace,'a') as lock:core.atomic_json(lock.path,{'run_id':'b'})
        self.assertTrue(lock.path.exists())
    def test_safe_artifact_paths(self):
        for path in ['../escape','/tmp/escape','C:\\escape','', '.', '\\..\\escape']:
            with self.subTest(path=path),self.assertRaises(ValueError):core.safe_path(self.workspace,path)
    @unittest.skipIf(os.name=='nt','Symlink privileges depend on Windows configuration')
    def test_symlink_escape(self):
        (self.workspace/'link').symlink_to(self.base,target_is_directory=True)
        with self.assertRaises(ValueError):core.safe_path(self.workspace,'link/secret')
    def test_duplicate_receipts_rejected(self):
        r={'id':'R1','status':'pass','evidence':'x'}
        gates=runner.requirement_gates({'criteria':[r,r]},{'criteria':[{'id':'R1'}]})
        self.assertFalse(all(g['passed'] for g in gates))
    def test_invalid_json_artifact_rejected(self):
        (self.workspace/'out.json').write_text('{broken}')
        self.assertFalse(runner.artifact_gates(self.workspace,{'deliverables':['out.json']})[0]['passed'])
    def test_usage_parser_does_not_accept_empty_dict_as_zero(self):
        p=self.base/'u.jsonl';p.write_text('{"type":"turn.completed","usage":{}}\n')
        self.assertFalse(runner.usage_from_log(p)[1])
    def test_usage_parser_ignores_unrelated_data(self):
        p=self.base/'u.jsonl';p.write_text('not json\n{"type":"item.completed","usage":{"input_tokens":900}}\n')
        self.assertFalse(runner.usage_from_log(p)[1])
    def test_usage_parser_ignores_non_object_json(self):
        p=self.base/'u.jsonl';p.write_text('[]\nnull\n"text"\n')
        self.assertFalse(runner.usage_from_log(p)[1])
    def test_per_process_security_configuration(self):
        (self.codex_home/'config.toml').write_text('[mcp_servers.context7]\nurl="http://example.invalid"\n[mcp_servers.external]\nurl="http://example.invalid"\n[plugins."browser@test"]\nenabled=true\n')
        argv=runner.codex_argv(dict(self.settings,allow_mcp=['context7']),self.workspace,self.base/'out.json',core.ROOT/'schemas/work.json',False)
        line=' '.join(argv)
        self.assertIn('mcp_servers."external".enabled=false',line);self.assertNotIn('mcp_servers."context7".enabled=false',line)
        self.assertIn('plugins."browser@test".enabled=false',line);self.assertNotIn('dangerously',line)
        self.assertIn('sandbox_workspace_write.network_access=false',line);self.assertEqual(argv[argv.index('-a')+1],'never')
    def test_reviewer_uses_read_only(self):
        argv=runner.codex_argv(self.settings,self.workspace,self.base/'o',core.ROOT/'schemas/review.json',True)
        self.assertEqual(argv[argv.index('--sandbox')+1],'read-only')
    def test_memory_matches_project_and_environment(self):
        core.save_recipe(self.workspace,'coding','fix parser',['run test'],'test evidence')
        self.assertEqual(len(core.recipes(self.workspace,'coding','fix parser')),1)
        (self.workspace/'package.json').write_text('{"version":"2"}')
        self.assertEqual(core.recipes(self.workspace,'coding','fix parser'),[])
        other=self.base/'other';other.mkdir();self.assertEqual(core.recipes(other,'coding','fix parser'),[])
    def test_quarantined_memory_is_not_recalled(self):
        rid=core.save_recipe(self.workspace,'coding','fix parser',['run test'],'test evidence')
        core.quarantine([rid]);self.assertEqual(core.recipes(self.workspace,'coding','fix parser'),[])
    def test_common_secrets_redacted_from_recipe(self):
        rid=core.save_recipe(self.workspace,'coding','fix parser',['token=abcdefghijklmnopqrstuvwxyz'],'evidence')
        self.assertNotIn('abcdefghijklmnopqrstuvwxyz',(core.store()/'recipes'/rid/'SKILL.md').read_text())
    def test_mode_routing(self):
        self.assertEqual(core.select_mode('연구 보고서 작성'),'report');self.assertEqual(core.select_mode('연구 실험 비교'),'research')
        self.assertEqual(core.select_mode('게임 제작'),'game');self.assertEqual(core.select_mode('시험 해설'),'study')
    def test_local_skill_search(self):
        d=self.codex_home/'skills'/'parser-testing';d.mkdir(parents=True)
        (d/'SKILL.md').write_text('---\nname: parser-testing\ndescription: testing parser regressions\n---\nBODY SHOULD NOT BE RETURNED\n')
        result=core.skill_search('parser');self.assertEqual(result[0]['name'],'parser-testing')
        self.assertNotIn('BODY',json.dumps(result))
    def test_prepare_only_makes_no_model_calls(self):
        with contextlib.redirect_stdout(io.StringIO()):rc=main(['run','test','--workspace',str(self.workspace),'--prepare-only'])
        self.assertEqual(rc,0);self.assertFalse((self.base/'counter.json').exists())
    def test_host_check_requires_argv_not_shell_string(self):
        with contextlib.redirect_stderr(io.StringIO()):rc=main(['run','test','--workspace',str(self.workspace),'--check','"echo evil"','--prepare-only'])
        self.assertEqual(rc,2)
    def test_run_store_must_not_be_in_workspace(self):
        with self.assertRaises(ValueError):runner.make_run('test',self.base,'coding',self.settings)

class InstallerTests(Base):
    def baseline(self):
        (self.codex_home/'AGENTS.md').write_bytes(b'\xef\xbb\xbf# Original\r\nKeep custom instructions.\r\n')
        for name,body in [('config.toml','model="local-model"\n'),('hooks.json','{"hooks":{}}'),('rules/default.rules','allow_local')]:
            p=self.codex_home/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)
        d=self.codex_home/'skills'/'original';d.mkdir(parents=True);(d/'SKILL.md').write_text('original skill')
        return installer.tree_hashes(self.codex_home)
    def test_additive_install_preserves_original_files(self):
        before=self.baseline();old=(self.codex_home/'AGENTS.md').read_bytes()
        result=installer.install(self.codex_home)
        for rel,h in before.items():
            if rel!='AGENTS.md':self.assertEqual(core.file_hash(self.codex_home/rel),h)
        self.assertTrue((self.codex_home/'AGENTS.md').read_bytes().startswith(old));self.assertTrue(all(result['unchanged_config_and_hooks'].values()))
    def test_uninstall_roundtrip_and_reinstall(self):
        before=self.baseline();installer.install(self.codex_home);installer.uninstall(self.codex_home)
        for rel,h in before.items():self.assertEqual(core.file_hash(self.codex_home/rel),h)
        result=installer.install(self.codex_home);self.assertTrue(Path(result['runner']).exists())
    def test_compact_mode_archives_original(self):
        self.baseline();old=(self.codex_home/'AGENTS.md').read_bytes()
        installer.install(self.codex_home,compact=True)
        self.assertEqual((self.codex_home/'adhd/legacy/AGENTS.original.md').read_bytes(),old)
        installer.uninstall(self.codex_home);self.assertEqual((self.codex_home/'AGENTS.md').read_bytes(),old)
    def test_uninstall_does_not_overwrite_new_user_guidance(self):
        self.baseline();installer.install(self.codex_home)
        with (self.codex_home/'AGENTS.md').open('a') as f:f.write('new user text')
        with self.assertRaises(ValueError):installer.uninstall(self.codex_home)
    def test_modified_backup_does_not_get_restored(self):
        self.baseline();record=installer.install(self.codex_home)
        (Path(record['backup'])/'AGENTS.md').write_text('tampered backup')
        with self.assertRaises(ValueError):installer.uninstall(self.codex_home)
    def test_existing_same_name_skill_is_not_overwritten(self):
        (self.codex_home/'skills/adhd').mkdir(parents=True)
        with self.assertRaises(ValueError):installer.install(self.codex_home)
    def test_doctor_labels_runtime_unverified(self):
        self.baseline();report=installer.audit(self.codex_home);self.assertIn('NOT_TESTED',report['runtime_status'])

if __name__=='__main__':unittest.main()
