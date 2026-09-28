from __future__ import annotations
import copy,json,os,subprocess,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from adhd.core import atomic_json,read_json,file_hash,store
from adhd.native import handle_event,session_key,folder,bridge,submit_request,checked_path,lease_path,DEFAULTS
from adhd.native_install import install_native,rollback_native,command_line
from adhd.models import route
from adhd.evidence import run_check
from adhd.vendor.smolagents_context import truncate_content
ROOT=Path(__file__).resolve().parents[1]

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.ws=self.base/'project';self.ws.mkdir();self.ch=self.base/'codex';self.ch.mkdir()
        self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.ch),'ADHD_EXEC_OWNER':''});self.env.start()
        self.sid='test-'+uuid.uuid4().hex;self.key=session_key(self.sid);self.counter=0
        self.event('UserPromptSubmit',prompt='Make a working program, preserve existing features and verify it.')
    def tearDown(self): self.env.stop();self.tmp.cleanup()
    def event(self,name,**kw):
        self.counter+=1
        return handle_event({'hook_event_name':name,'session_id':self.sid,'cwd':str(self.ws),'turn_id':str(self.counter),'model':'gpt-6-sol',**kw})
    def state(self): return read_json(folder(self.key)/'state.json')
    def save(self,s): atomic_json(folder(self.key)/'state.json',s)
    def request(self,op,payload):
        req=submit_request(self.key,self.ws,op,payload);self.event('PostToolUse',tool_name='Bash')
        return read_json(Path(req['receipt']))
    def begin(self,kind='test'):
        ack=self.request('begin',{'mode':'coding','criteria':[{'id':'R1','text':'Behavior passes','kind':kind}],'artifacts':['answer.txt'],'plan':{'objective':'Preserve behavior','approach':'Inspect and implement minimal fix','verification':'Run regression tests','preflight':['Inspect baseline'],'risks':['Regression risk'],'alternatives':['Small focused change rather than rewrite'],'steps':[{'id':'S1','action':'Implement then test','requirements':['R1'],'depends_on':[]}]}})
        self.assertTrue(ack['ok'],ack);return ack
    def candidate(self,**extra):
        (self.ws/'answer.txt').write_text('verified result')
        receipt=run_check({'run_id':self.state()['run_id'],
                           'contract_revision':self.state()['intent_version'],
                           'subject_paths':['answer.txt'],
                           'argv':[sys.executable,'-c','print("ok")']},self.ws)
        payload={'files':['answer.txt'],'criterion_results':[{'id':'R1','pass':True,'evidence':'Observed local process with exit code 0','evidence_ids':[receipt['receipt']]}],'sources':[],'procedure':['Run the correct test then inspect the output']};payload.update(extra)
        return self.request('candidate',payload)
    def spawn(self,role='adhd-verifier',model=None,aid='child'):
        tool_use_id=uuid.uuid4().hex
        out=self.event('PreToolUse',tool_name='spawn_agent',tool_use_id=tool_use_id,tool_input={'agent_type':role,'message':'Read the given current candidate.'})
        self.assertNotEqual(out.get('hookSpecificOutput',{}).get('permissionDecision'),'deny',out)
        self.event('SubagentStart',agent_type=role,agent_id=aid,model=model or route(role)['model'],tool_use_id=tool_use_id)
    def review(self,approve=True,aid='child',**kw):
        c=self.state()['candidate']
        s=self.state()
        data={'verdict':'approve' if approve else 'reject','reviewed_digest':c['digest'],
              'reviewed_contract_hash':s['contract_hash'],
              'reviewed_turn_ids':[p['turn_id'] for p in s['prompts']],
              'intent_alignment':True,
              'criterion_results':c['criterion_results'],'findings':[] if approve else ['Real test is missing']};data.update(kw)
        return self.event('SubagentStop',agent_type='adhd-verifier',agent_id=aid,last_assistant_message=json.dumps(data))
    def installed_verifier_profile(self):
        target=self.ch/'agents'/'adhd-verifier.toml';target.parent.mkdir(parents=True,exist_ok=True)
        template=(ROOT/'native'/'agents'/'adhd-verifier.toml').read_text(encoding='utf-8')
        target.write_text(template.replace('ADHD_ROOT',str(ROOT)),encoding='utf-8')
        return target
    def observed_verifier_start(self,**kw):
        self.installed_verifier_profile()
        self.event('SessionStart')
        self.event('SubagentStart',agent_type='adhd-verifier',agent_id='app-reviewer',
                    model='gpt-6-sol',**kw)
    def test_idle_does_not_loop(self): self.assertEqual(self.event('Stop'),{})
    def test_post_compact_uses_supported_output_and_keeps_state(self):
        self.begin()
        before=self.state()
        out=self.event('PostCompact',trigger='auto')
        self.assertEqual(out,{})
        self.assertEqual(self.state()['run_id'],before['run_id'])
        self.assertEqual(read_json(bridge(self.ws,self.key)/'view.json')['status'],'working')
    def test_post_compact_command_prints_only_supported_json(self):
        event={'hook_event_name':'PostCompact','session_id':self.sid,
               'cwd':str(self.ws),'turn_id':'compact-1','model':'gpt-6-sol','trigger':'auto'}
        run=subprocess.run([sys.executable,str(ROOT/'hook.py')],input=json.dumps(event),
                           text=True,capture_output=True,env=os.environ,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(run.stderr,'')
        self.assertEqual(json.loads(run.stdout),{})
        self.assertTrue((bridge(self.ws,self.key)/'view.json').exists())
    def test_subagent_stop_uses_supported_output_after_review(self):
        self.begin();self.assertTrue(self.candidate()['ok']);self.spawn()
        out=self.review()
        self.assertEqual(out,{})
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['review_receipt']['verdict']['verdict'],'approve')
    def test_request_is_queued_not_applied(self):
        submit_request(self.key,self.ws,'begin',{})
        self.assertEqual(self.state()['status'],'idle')
    def test_native_success(self):
        self.begin();self.assertTrue(self.candidate()['ok']);self.spawn();self.review()
        s=self.state();self.assertEqual(s['status'],'complete');self.assertTrue(s['recipe_saved']);self.assertFalse(lease_path(self.ws).exists());self.assertFalse(self.event('Stop')['continue']);self.assertEqual(self.event('Stop'),{})
    def test_app_observed_lifecycle_can_complete_without_pretooluse(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        self.observed_verifier_start()
        self.review(aid='app-reviewer')
        state=self.state()
        self.assertEqual(state['status'],'complete')
        self.assertEqual(state['review_receipt']['agent_id'],'app-reviewer')
        self.assertEqual(state['review_receipt']['correlation'],'host_agent_lifecycle')
        self.assertEqual(state['review_receipt']['configured_effort'],'max')
        self.assertIsNone(state['review_receipt']['observed_effort'])
        self.assertEqual(state['review_receipt']['effort_source'],'pinned_role_profile')
    def test_verifier_spawn_rejects_conflicting_effort_override(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        out=self.event('PreToolUse',tool_name='spawn_agent',tool_use_id='wrong-effort',
                       tool_input={'agent_type':'adhd-verifier','reasoning_effort':'low',
                                   'message':'Review the current candidate.'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
        self.assertFalse(self.state()['reservations'])
    def test_app_observed_lifecycle_rejects_wrong_effort(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        self.observed_verifier_start(reasoning_effort='low')
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_observed_lifecycle_rejects_profile_change(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        self.observed_verifier_start()
        (self.ch/'agents'/'adhd-verifier.toml').write_text('model = "gpt-6-luna"')
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_lifecycle_rejects_hot_upgrade_after_session_start(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        installed=self.installed_verifier_profile()
        self.event('SessionStart')
        new_root=self.base/'next-release';profile=new_root/'native'/'agents'/'adhd-verifier.toml'
        profile.parent.mkdir(parents=True)
        template=(ROOT/'native'/'agents'/'adhd-verifier.toml').read_text(encoding='utf-8')
        template=template.replace('Independent READ-ONLY reviewer.','Independent READ-ONLY reviewer. New release.')
        profile.write_text(template,encoding='utf-8')
        installed.write_text(template.replace('ADHD_ROOT',str(new_root)),encoding='utf-8')
        with patch('adhd.native.ROOT',new_root):
            self.event('SubagentStart',agent_type='adhd-verifier',agent_id='app-reviewer',model='gpt-6-sol')
            self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_lifecycle_requires_session_start_attestation(self):
        self.begin();self.assertTrue(self.candidate()['ok']);self.installed_verifier_profile()
        self.event('SubagentStart',agent_type='adhd-verifier',agent_id='app-reviewer',model='gpt-6-sol')
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_observed_lifecycle_requires_profile(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        self.event('SubagentStart',agent_type='adhd-verifier',agent_id='app-reviewer',model='gpt-6-sol')
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_observed_lifecycle_rejects_wrong_model(self):
        self.begin();self.assertTrue(self.candidate()['ok']);self.installed_verifier_profile()
        self.event('SessionStart')
        self.event('SubagentStart',agent_type='adhd-verifier',agent_id='app-reviewer',model='gpt-6-luna')
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_observed_lifecycle_rejects_duplicate_agent_id(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        self.observed_verifier_start()
        self.event('SubagentStart',agent_type='adhd-verifier',agent_id='app-reviewer',model='gpt-6-sol')
        self.review(aid='app-reviewer')
        self.assertEqual(self.state()['status'],'revising')
    def test_app_observed_lifecycle_rejects_mismatched_stop_role(self):
        self.begin();self.assertTrue(self.candidate()['ok'])
        self.observed_verifier_start()
        candidate=self.state()['candidate'];state=self.state()
        verdict={'verdict':'approve','reviewed_digest':candidate['digest'],
                 'reviewed_contract_hash':state['contract_hash'],
                 'reviewed_turn_ids':[p['turn_id'] for p in state['prompts']],
                 'intent_alignment':True,'criterion_results':candidate['criterion_results'],'findings':[]}
        self.event('SubagentStop',agent_type='adhd-scout',agent_id='app-reviewer',
                   last_assistant_message=json.dumps(verdict))
        self.assertEqual(self.state()['status'],'revising')
    def test_worker_cannot_self_approve(self):
        self.begin();self.candidate();self.review(aid='unobserved-forged-agent')
        self.assertEqual(self.state()['status'],'reviewing');self.assertEqual(self.event('Stop')['decision'],'block')
    def test_wrong_verifier_model_rejected(self):
        self.begin();self.candidate();self.spawn(model='gpt-6-luna');self.review();self.assertEqual(self.state()['status'],'revising')
    def test_verifier_must_review_original_intent(self):
        self.begin();self.candidate();self.spawn();self.review(intent_alignment=False)
        self.assertEqual(self.state()['status'],'revising')
    def test_verifier_reject_then_repair(self):
        self.begin();self.candidate();self.spawn();self.review(False);self.assertEqual(self.state()['status'],'revising')
        self.candidate();self.spawn(aid='second');self.review(aid='second');self.assertEqual(self.state()['status'],'complete')
    def test_edits_after_review_invalidated(self):
        self.begin();self.candidate();self.spawn();self.review();(self.ws/'answer.txt').write_text('later edit')
        self.assertEqual(self.event('Stop')['decision'],'block');self.assertEqual(self.state()['status'],'revising')
    def test_edits_during_review_invalidated(self):
        self.begin();self.candidate();self.spawn();(self.ws/'answer.txt').write_text('race');self.review();self.assertEqual(self.state()['status'],'revising')
    def test_wrong_digest_rejected(self):
        self.begin();self.candidate();self.spawn();self.review(reviewed_digest='wrong');self.assertEqual(self.state()['status'],'revising')
    def test_missing_criterion_rejected(self):
        self.begin();self.assertFalse(self.candidate(criterion_results=[])['ok'])
    def test_duplicate_criterion_rejected(self):
        self.begin();row={'id':'R1','pass':True,'evidence':'evidence'};self.assertFalse(self.candidate(criterion_results=[row,row])['ok'])
    def test_empty_evidence_rejected(self):
        self.begin();self.assertFalse(self.candidate(criterion_results=[{'id':'R1','pass':True,'evidence':''}])['ok'])
    def test_empty_artifact_rejected(self):
        self.begin();(self.ws/'answer.txt').write_text('')
        self.assertFalse(self.request('candidate',{'criterion_results':[{'id':'R1','pass':True,'evidence':'x'}]})['ok'])
    def test_source_ledger_required(self):
        self.begin('source');self.assertFalse(self.candidate()['ok'])
        (self.ws/'source.txt').write_text('Cited text',encoding='utf-8')
        self.assertTrue(self.candidate(sources=[{'claim_id':'R1','claim':'X','source':'source.txt',
            'locator':'L1','basis':'read','evidence_ref':'source.txt'}])['ok'])
        (self.ws/'source.txt').write_text('Changed citation',encoding='utf-8')
        out=self.event('PreToolUse',tool_name='spawn_agent',tool_use_id='stale-source',
                       tool_input={'agent_type':'adhd-verifier'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_original_prompt_not_replaced_by_stop(self):
        self.begin();s=self.state();out=self.event('Stop');self.event('UserPromptSubmit',prompt=out['reason'])
        self.assertEqual(self.state()['prompts'],s['prompts']);self.assertEqual(self.state()['intent_version'],s['intent_version'])
    def test_amendment_requires_sync(self):
        self.begin();self.event('UserPromptSubmit',prompt='Also support offline operation.')
        self.assertFalse(self.candidate()['ok']);self.assertTrue(self.request('sync-intent',{'additions':[{'id':'R2','text':'Offline','kind':'behavior'}]})['ok'])
        self.assertEqual(len(self.state()['contract']['criteria']),2)
    def test_agent_cannot_weaken_contract(self):
        self.begin();self.assertFalse(self.request('sync-intent',{'additions':[{'id':'R1','text':'Just create file','kind':'artifact'}]})['ok'])
    def test_stop_resume_keeps_intent(self):
        self.begin();c=self.state()['contract'];self.event('UserPromptSubmit',prompt='그만')
        self.assertFalse(self.event('Stop')['continue']);self.assertEqual(self.event('Stop'),{})
        self.event('UserPromptSubmit',prompt='재개');self.assertEqual(self.state()['contract'],c);self.assertEqual(self.state()['status'],'working')
    def test_pause_cannot_reset_budget(self):
        self.begin();self.request('pause',{});self.assertFalse(self.request('begin',{'mode':'coding'})['ok'])
    def test_session_end_pauses(self):
        self.begin();self.event('SessionEnd');self.assertEqual(self.state()['status'],'paused');self.assertFalse(lease_path(self.ws).exists())
    def test_max_rounds(self):
        self.begin();s=self.state();s['policy']['max_rounds']=1;self.save(s)
        self.assertEqual(self.event('Stop')['decision'],'block');self.assertFalse(self.event('Stop')['continue']);self.assertEqual(self.state()['status'],'budget_exhausted')
    def test_duplicate_stop_is_idempotent(self):
        self.begin();a=self.event('Stop',turn_id='fixed');b=self.event('Stop',turn_id='fixed')
        self.assertEqual(a,b);self.assertEqual(self.state()['rounds'],1)
    def test_wall_clock_budget(self):
        self.begin();s=self.state();s['started']-=7200;self.save(s);self.assertFalse(self.event('Stop')['continue'])
    def test_native_tokens_not_fabricated(self): self.assertIsNone(self.state()['usage']['tokens']);self.assertFalse(self.state()['usage']['observed'])
    def test_astra_once(self):
        self.begin();self.spawn('adhd-architect',aid='A');self.event('SubagentStop',agent_type='adhd-architect',agent_id='A',last_assistant_message='decision')
        out=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'adhd-architect','message':'another'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_astra_brief_limit(self):
        self.begin();out=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'adhd-architect','message':'x'*8001})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny');self.assertEqual(self.state()['astra_calls'],0)
    def test_parallel_limit(self):
        self.begin()
        for i in range(3): self.spawn('adhd-scout',aid=str(i))
        out=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'adhd-scout'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_one_delegated_writer(self):
        self.begin();self.spawn('adhd-implementer');out=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'adhd-implementer'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_explicit_model_mismatch_denied(self):
        self.begin();out=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'adhd-scout','model':'gpt-6-astra'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_child_cannot_spawn(self):
        self.begin();out=self.event('PreToolUse',model='gpt-6-astra',tool_name='spawn_agent',tool_input={'agent_type':'adhd-scout'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
    def test_native_and_legacy_exclusion(self):
        p=lease_path(self.ws);atomic_json(p,{'run_id':'legacy-running'})
        ack=self.request('begin',{'criteria':[{'id':'R1','text':'x','kind':'test'}],'artifacts':['a']})
        self.assertFalse(ack['ok']);self.assertEqual(read_json(p)['run_id'],'legacy-running')
    def test_malformed_mailbox(self):
        p=bridge(self.ws,self.key)/'inbox'/(uuid.uuid4().hex+'.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{')
        self.event('PostToolUse');self.assertEqual(self.state()['status'],'idle')
    def test_paths(self):
        for path in ['../secret','/etc/passwd','C:\\Windows\\file','a/../../b']:
            with self.subTest(path=path),self.assertRaises(ValueError): checked_path(self.ws,path)
    def test_internal_symlink_blocked(self):
        (self.ws/'a').write_text('a')
        try: (self.ws/'b').symlink_to(self.ws/'a')
        except OSError: self.skipTest('OS does not allow test symlink')
        with self.assertRaises(ValueError): checked_path(self.ws,'b',existing=True)
    def test_new_prompt_after_complete_is_new_task(self):
        self.begin();self.candidate();self.spawn();self.review();self.event('UserPromptSubmit',prompt='A different deliverable now')
        pending=self.state()['pending_turn_ids'][0]
        self.assertTrue(self.request('sync-intent',{'source_turn_id':pending,
            'base_revision':self.state()['intent_version'],'classification':'new_task'})['ok'])
        self.assertEqual(self.state()['status'],'idle');self.assertEqual(len(self.state()['prompts']),1)
    def test_hook_subprocess_json(self):
        ev={'hook_event_name':'UserPromptSubmit','session_id':'subprocess','cwd':str(self.ws),'prompt':'hello','model':'gpt-6-sol','turn_id':'sp'}
        p=subprocess.run([sys.executable,str(ROOT/'hook.py')],input=json.dumps(ev),text=True,capture_output=True,env=os.environ,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr);self.assertIn('hookSpecificOutput',json.loads(p.stdout))
    def test_legacy_child_does_not_arm_native(self):
        with patch.dict(os.environ,{'ADHD_EXEC_OWNER':'legacy'}):self.assertEqual(self.event('UserPromptSubmit',prompt='legacy'),{})
    def test_vendor_compaction_hard_character_limit(self):
        for n in [0,1,50,200,5000]:self.assertLessEqual(len(truncate_content('A'*4000+'Z'*4000,n)),n)
        self.assertTrue(truncate_content('A'*4000+'Z'*4000,200).endswith('Z'))
    def test_no_source_requirement_can_be_falsified_by_view(self):
        self.begin();p=bridge(self.ws,self.key)/'view.json';v=read_json(p);v['status']='complete';atomic_json(p,v)
        self.event('Stop');self.assertNotEqual(self.state()['status'],'complete')

    def test_invalid_assumptions_rejected(self):
        ack=self.request('begin',{'mode':'coding','criteria':[{'id':'R1','text':'test','kind':'test'}],'artifacts':['a.txt'],'assumptions':{'secret':'not a list'}})
        self.assertFalse(ack['ok'])
    def test_host_state_not_inside_workspace(self):
        self.ws=self.base; self.sid='parent-folder'; self.key=session_key(self.sid)
        self.event('UserPromptSubmit',prompt='Make a program')
        ack=self.request('begin',{'mode':'coding','criteria':[{'id':'R1','text':'test','kind':'test'}],'artifacts':['a.txt']})
        self.assertFalse(ack['ok']);self.assertIn('host state',ack['message'])
    def test_malformed_hook_json_type(self):
        p=subprocess.run([sys.executable,str(ROOT/'hook.py')],input='[]',text=True,capture_output=True,env=os.environ,timeout=10)
        self.assertEqual(p.returncode,0);self.assertIn('systemMessage',json.loads(p.stdout))
    def test_final_verifier_slot_reserved(self):
        self.begin();self.candidate();s=self.state();s['total_children']=11;self.save(s)
        out=self.event('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'adhd-scout','message':'read'})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'],'deny')
        self.spawn();self.review();self.assertEqual(self.state()['status'],'complete')
    def test_two_native_sessions_cannot_write_same_workspace(self):
        self.begin();owner=read_json(lease_path(self.ws))
        self.sid='other-session';self.key=session_key(self.sid);self.event('UserPromptSubmit',prompt='A competing edit')
        ack=self.request('begin',{'mode':'coding','criteria':[{'id':'R1','text':'test','kind':'test'}],'artifacts':['a.txt']})
        self.assertFalse(ack['ok']);self.assertEqual(read_json(lease_path(self.ws)),owner)

class NativeInstallTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.b=Path(self.t.name);self.c=self.b/'codex';self.c.mkdir();self.a=self.b/'agents'
        (self.c/'config.toml').write_text('# keep comment\nmodel = "gpt-5.6-sol"\n[model_providers.router]\nbase_url = "http://127.0.0.1:4202/v1"\n[mcp_servers.context7]\ncommand = "node"\n')
        (self.c/'AGENTS.md').write_text('Original user integrations\n')
        (self.c/'hooks.json').write_text(json.dumps({'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'old-hook','trusted_hash':'existing-do-not-change'}]}]}}))
        (self.c/'agents').mkdir();(self.c/'agents'/'researcher.toml').write_text('name="researcher"\ndescription="legacy"\nmodel="gpt-5.6-terra"\ndeveloper_instructions="Preserve old instructions"\n')
        self.original={str(p.relative_to(self.c)):p.read_bytes() for p in self.c.rglob('*') if p.is_file()}
    def tearDown(self):self.t.cleanup()
    def test_install_restore(self):
        result=install_native(self.c,self.a);self.assertEqual(result['migrated_roles'],[])
        import tomllib
        self.assertEqual(route('adhd-planner')['effort'],'max')
        self.assertEqual(route('adhd-scout')['effort'],'max')
        self.assertEqual(route('adhd-architect')['effort'],'low')
        cfg=tomllib.loads((self.c/'config.toml').read_text());self.assertEqual(cfg['model'],'gpt-5.6-sol');self.assertEqual((self.c/'config.toml').read_bytes(),self.original['config.toml']);self.assertEqual(cfg['model_providers']['router']['base_url'],'http://127.0.0.1:4202/v1')
        self.assertEqual((self.c/'agents/researcher.toml').read_bytes(),self.original[str(Path('agents')/'researcher.toml')])
        hooks=read_json(self.c/'hooks.json');self.assertEqual(hooks['hooks']['SessionStart'][0]['hooks'][0]['trusted_hash'],'existing-do-not-change')
        self.assertNotIn('trusted_hash',hooks['hooks']['Stop'][-1]['hooks'][0]);self.assertLessEqual(hooks['hooks']['SessionEnd'][-1]['hooks'][0]['timeout'],3)
        self.assertNotIn('additionalContextLimit',hooks['hooks']['SubagentStop'][-1]['hooks'][0])
        self.assertNotIn('additionalContextLimit',hooks['hooks']['PostCompact'][-1]['hooks'][0])
        self.assertEqual(len(list((self.c/'agents').glob('adhd-*.toml'))),6)
        for name in ('planner','scout','light','implementer','verifier'):
            role=tomllib.loads((self.c/'agents'/f'adhd-{name}.toml').read_text(encoding='utf-8'))
            self.assertEqual(role['model_reasoning_effort'],'max')
        self.assertNotIn('ADHD_ROOT',(Path(result['release'])/'skills/adhd-native/SKILL.md').read_text(encoding='utf-8'));self.assertNotIn('ADHD_PYTHON',(self.a/'skills/adhd-native/SKILL.md').read_text(encoding='utf-8'));
        self.assertTrue((self.a/'skills/adhd-native/SKILL.md').exists());self.assertTrue((self.c/'AGENTS.md').read_bytes().startswith(self.original['AGENTS.md']))
        rollback_native(self.c)
        for p,v in self.original.items():self.assertEqual((self.c/p).read_bytes(),v,p)
    def test_rollback_refuses_later_user_edit(self):
        install_native(self.c,self.a);(self.c/'AGENTS.md').write_text('User modified after install')
        with self.assertRaises(ValueError):rollback_native(self.c)
        self.assertEqual((self.c/'AGENTS.md').read_text(),'User modified after install')
    def test_second_install_refused(self):
        install_native(self.c,self.a)
        with self.assertRaises(ValueError):install_native(self.c,self.a)
    def test_compact_preserves_original_separately(self):
        record=install_native(self.c,self.a,compact=True)
        archives=list((self.c/'adhd/legacy').glob('AGENTS-*'));self.assertEqual(archives[0].read_bytes(),self.original['AGENTS.md'])
        rollback_native(self.c);self.assertEqual((self.c/'AGENTS.md').read_bytes(),self.original['AGENTS.md'])
    def test_sanitized_config_refused(self):
        (self.c/'config.toml').write_text('model="gpt-6-sol"\npath="<USER_HOME>"')
        with self.assertRaises(ValueError):install_native(self.c,self.a)
    def test_keep_legacy_models_flag(self):
        result=install_native(self.c,self.a,migrate_models=False);self.assertEqual(result['migrated_roles'],[])
        self.assertIn('gpt-5.6-terra',(self.c/'agents/researcher.toml').read_text())
    def test_windows_quote(self):
        s=command_line(['C:\\Program Files\\Python\\python.exe','C:\\some folder\\hook.py'],True)
        self.assertIn('"C:\\Program Files',s)
        with self.assertRaises(ValueError):command_line(['x\ny'],True)
    def test_all_agent_toml_parse(self):
        import tomllib
        for p in (ROOT/'native/agents').glob('*.toml'):
            d=tomllib.loads(p.read_text());self.assertEqual(d['model'],route(d['name'])['model']);self.assertIn('developer_instructions',d)

if __name__=='__main__':unittest.main()
