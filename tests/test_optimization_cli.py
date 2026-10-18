import base64
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_maintenance_budget_failure_is_an_explicit_cli_error(self):
        from adhd.cli import main
        from adhd.maintenance_budget import BudgetExhausted
        stderr = io.StringIO()
        with patch('adhd.extra_cli.execute', side_effect=BudgetExhausted('maintenance-cpu-budget-exhausted')):
            with redirect_stderr(stderr):
                result = main(['snapshot', 'capabilities'])
        self.assertEqual(result, 2)
        self.assertEqual('Error: maintenance-cpu-budget-exhausted\n', stderr.getvalue())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws=Path(self.temp.name)
        self.env={**os.environ,'CODEX_HOME':str(self.ws/'home')}

    def call(self,*args):
        result=subprocess.run([sys.executable,str(ROOT/'adhd.py'),*map(str,args)],cwd=self.ws,
                              env=self.env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_snapshot_and_trial_commands_reach_their_implementations(self):
        self.assertFalse(self.call('snapshot','capabilities')['enabled'])
        self.assertEqual(self.call('experiment','summary','--root',self.ws/'trials')['started'],0)

    def test_context_cli_finds_required_symbol_and_reads_a_revision(self):
        (self.ws/'target.py').write_text('def required_symbol():\n    return 42\n')
        located=self.call('context','locate','--workspace',self.ws,'--query','required_symbol','--sources','code','--complete')
        self.assertTrue(any(card['ref']['path']=='target.py' for card in located['cards']))
        ref=next(card['ref'] for card in located['cards'] if card['ref']['path']=='target.py')
        payload=self.ws/'read.json'
        payload.write_text(json.dumps({'ref':ref,'expected_revision':ref['revision']}))
        read=self.call('context','read','--workspace',self.ws,'--payload-file',payload)
        self.assertEqual(read['status'],'ok')
        self.assertIn('required_symbol',read['text'])
        fragment=self.call('context','read','--workspace',self.ws,'--payload-file',payload,
                           '--start-char',5,'--max-chars',7)
        self.assertEqual(fragment['text'],read['text'][5:12])

    def test_diagnostics_and_object_audit_are_bound_to_cli(self):
        payload=self.ws/'capabilities.json'
        payload.write_text(json.dumps({'configured':['example'],'exposed':[]}))
        result=self.call('diagnose','capabilities','--payload-file',payload)
        self.assertIsInstance(result,dict)
        audit=self.call('objects','audit','--root',self.ws/'objects')
        self.assertEqual(audit['objects'],0)
        self.assertIsNone(audit['reclaimed_disk_bytes'])

    def test_object_cli_redacts_filesystem_error_paths_for_every_action(self):
        from adhd.cli import main
        digest='a'*64
        private_path=self.ws/'another-workspace'/'objects'/digest
        for action in ('capture','read','export','restore','forget','audit','gc','apply','recover'):
            with self.subTest(action=action):
                stderr=io.StringIO()
                with patch('adhd.extra_cli.execute',side_effect=PermissionError(13,'access denied',str(private_path))):
                    with redirect_stderr(stderr):
                        result=main(['objects',action,'--root',str(self.ws/'store')])
                self.assertEqual(result,2)
                self.assertEqual(stderr.getvalue(),'Error: object-access-or-filesystem-unavailable\n')
                self.assertNotIn(digest,stderr.getvalue())
                self.assertNotIn(str(private_path),stderr.getvalue())

    def test_object_collection_requires_a_persisted_operation(self):
        root=self.ws/'objects'
        plan=self.call('objects','gc','--root',root)
        self.assertTrue(plan['dry_run'])
        self.assertEqual(plan.get('removed',0),0)
        self.assertEqual(plan['status'],'blocked')
        recovered=subprocess.run([sys.executable,str(ROOT/'adhd.py'),'objects','recover',
            '--root',str(root)],cwd=self.ws,env=self.env,capture_output=True,
            text=True,encoding='utf-8',timeout=60)
        self.assertNotEqual(recovered.returncode,0)
        self.assertIn('registered-roots',recovered.stdout+recovered.stderr)
        missing=subprocess.run([sys.executable,str(ROOT/'adhd.py'),'objects','apply',
            '--root',str(root)],cwd=self.ws,env=self.env,capture_output=True,
            text=True,encoding='utf-8',timeout=60)
        self.assertNotEqual(missing.returncode,0)
        self.assertIn('--operation-id',missing.stdout+missing.stderr)

    def test_context_read_enforces_no_global(self):
        from adhd.memory import Memory
        with patch.dict(os.environ,self.env):
            with Memory() as memory:
                memory.put(scope='global',kind='fact',key='shared-evidence',
                           content='required global fixture body',source='test',locator='fixture')
        located=self.call('context','locate','--workspace',self.ws,'--query','shared-evidence',
                          '--sources','memory','--environment','fixture')
        ref=next(card['ref'] for card in located['cards'] if card['ref']['source']=='memory')
        payload=self.ws/'global-ref.json'
        payload.write_text(json.dumps({'ref':ref,'expected_revision':ref['revision']}))
        allowed=self.call('context','read','--workspace',self.ws,'--payload-file',payload,
                          '--environment','fixture')
        self.assertIn('required global fixture body',allowed['text'])
        denied=subprocess.run([sys.executable,str(ROOT/'adhd.py'),'context','read','--workspace',
            str(self.ws),'--payload-file',str(payload),'--no-global','--environment','fixture'],
            cwd=self.ws,env=self.env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertNotIn('required global fixture body',denied.stdout+denied.stderr)
        if denied.returncode==0:
            self.assertNotEqual(json.loads(denied.stdout).get('status'),'ok')

    def test_completed_native_capture_read_export_forget_and_standalone_restore(self):
        from test_native import NativeTests
        native=NativeTests('test_native_success')
        native.setUp()
        self.addCleanup(native.tearDown)
        native.begin()
        self.assertTrue(native.candidate()['ok'])
        native.spawn()
        native.review()
        self.assertEqual(native.state()['status'],'complete')
        self.ws=native.ws
        self.env={**os.environ,'CODEX_HOME':str(native.ch)}
        (self.ws/'.adhd'/'immutable-policy.json').write_text(json.dumps({
            'schema_version':1,'policy_revision':'cli-policy','allowed_privacy':['private'],
            'allowed_actions':['capture','read','export','restore','forget']}))
        source=native.ch/'adhd'/'native'/native.key/('completion-'+native.state()['run_id']+'.json')
        request=self.ws/'capture-request.json'
        request.write_text(json.dumps({'owner_key':native.key,'policy_revision':'cli-policy'}))
        captured=self.call('objects','capture','--workspace',self.ws,'--payload-file',request)
        access={'ref_id':captured['ref_id'],'policy_revision':'cli-policy','allowed_privacy':['private']}
        request.write_text(json.dumps(access))
        body=self.call('objects','read','--workspace',self.ws,'--payload-file',request)
        self.assertEqual(base64.b64decode(body['data']),source.read_bytes())
        request.write_text(json.dumps({**access,'allowed_privacy':['private','sensitive']}))
        expanded=subprocess.run([sys.executable,str(ROOT/'adhd.py'),'objects','read','--workspace',
            str(self.ws),'--payload-file',str(request)],cwd=self.ws,env=self.env,
            capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertNotEqual(expanded.returncode,0)
        self.assertNotIn(body['data'],expanded.stdout+expanded.stderr)
        destination=self.ws/'completed-evidence.zip'
        request.write_text(json.dumps({**access,'destination':str(destination)}))
        self.call('objects','export','--workspace',self.ws,'--payload-file',request)
        request.write_text(json.dumps(access))
        self.assertTrue(self.call('objects','forget','--workspace',self.ws,'--payload-file',request)['forgotten'])
        denied=subprocess.run([sys.executable,str(ROOT/'adhd.py'),'objects','read','--workspace',str(self.ws),
            '--payload-file',str(request)],cwd=self.ws,env=self.env,capture_output=True,
            text=True,encoding='utf-8',timeout=60)
        self.assertNotEqual(denied.returncode,0)
        self.assertIn('reference-unavailable',denied.stdout+denied.stderr)
        restored=self.ws/'restored'
        restored.mkdir()
        request.write_text(json.dumps({'policy_revision':'cli-policy','allowed_privacy':['private'],
            'export_path':str(destination),'destination':str(restored/source.name)}))
        self.call('objects','restore','--workspace',self.ws,'--payload-file',request)
        self.assertEqual((restored/source.name).read_bytes(),source.read_bytes())


if __name__=='__main__':unittest.main()
