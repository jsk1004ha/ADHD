"""Regression fixtures for the additional v0.1.2 task guarantees."""
from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from contextlib import asynccontextmanager
import sys
import zipfile
import os
import shutil
import uuid

from apzn.core import digest, file_hash


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, content in {
            'raw.csv': 'mass\n1.5\n2.5\n',
            'analysis.py': 'print((1.5 + 2.5) / 2)\n',
            'result.json': '{"mean_mass": 2.0}\n',
            'chart.svg': '<svg><text>2.0 kg</text></svg>\n',
            'report.md': 'Measured mean mass: 2.0 kg.\n',
        }.items():
            (self.root / name).write_text(content, encoding='utf-8')
        self.manifest = {
            'schema': 1,
            'nodes': [
                {'id': 'raw', 'role': 'raw', 'path': 'raw.csv', 'sha256': file_hash(self.root / 'raw.csv'), 'inputs': []},
                {'id': 'code', 'role': 'analysis', 'path': 'analysis.py', 'sha256': file_hash(self.root / 'analysis.py'), 'inputs': ['raw']},
                {'id': 'result', 'role': 'result', 'path': 'result.json', 'sha256': file_hash(self.root / 'result.json'), 'inputs': ['raw', 'code']},
                {'id': 'chart', 'role': 'visual', 'path': 'chart.svg', 'sha256': file_hash(self.root / 'chart.svg'), 'inputs': ['result']},
                {'id': 'report', 'role': 'report', 'path': 'report.md', 'sha256': file_hash(self.root / 'report.md'), 'inputs': ['result', 'chart']},
            ],
            'claims': [{'id': 'mean', 'category': 'calculated', 'result': 'result',
                        'json_pointer': '/mean_mass', 'output': 'report',
                        'literal': '2.0 kg', 'value': '2.0', 'unit': 'kg'}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def _write(self):
        path = self.root / 'provenance.json'
        path.write_text(json.dumps(self.manifest), encoding='utf-8')
        return path

    def test_real_hashed_chain_and_numeric_claim(self):
        from apzn.provenance import validate_provenance
        result = validate_provenance(self.root, self._write())
        self.assertEqual(result['verified_claims'], ['mean'])
        self.assertEqual(result['coverage'], 'declared_claims_only')

    def test_mismatched_report_number_rejected(self):
        from apzn.provenance import validate_provenance
        self.manifest['claims'][0]['value'] = '2.1'
        with self.assertRaises(ValueError):
            validate_provenance(self.root, self._write())

    def test_stale_raw_hash_rejected(self):
        from apzn.provenance import validate_provenance
        path = self._write()
        (self.root / 'raw.csv').write_text('mass\n9.0\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            validate_provenance(self.root, path)

    def test_raw_file_cannot_impersonate_analysis_code(self):
        from apzn.provenance import validate_provenance
        self.manifest['nodes'][1].update(path='raw.csv',sha256=file_hash(self.root/'raw.csv'))
        with self.assertRaisesRegex(ValueError,'distinct artifact paths'):
            validate_provenance(self.root,self._write())

    def test_noncode_analysis_file_is_rejected(self):
        from apzn.provenance import validate_provenance
        extra=self.root/'other.csv';extra.write_text('1,2\n',encoding='utf-8')
        self.manifest['nodes'][1].update(path='other.csv',sha256=file_hash(extra))
        with self.assertRaisesRegex(ValueError,'code artifact'):
            validate_provenance(self.root,self._write())

    def test_declared_presentation_number_uses_native_text(self):
        from apzn.provenance import validate_provenance
        slide = self.root / 'slides.pptx'
        with zipfile.ZipFile(slide, 'w') as archive:
            archive.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="urn:p"/>')
            archive.writestr('ppt/slides/slide1.xml',
                '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:sp><a:t>2.0 kg</a:t></p:sp></p:sld>')
        self.manifest['nodes'][-1].update(path='slides.pptx',sha256=file_hash(slide))
        self.manifest['claims'][0]['literal'] = '2.0 kg'
        self.assertEqual(validate_provenance(self.root,self._write())['verified_claims'],['mean'])


class LargeArtifactTests(unittest.TestCase):
    def test_explicit_large_artifact_uses_separate_streamed_budget(self):
        from apzn.snapshots import build_snapshot, validate_snapshot
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'model.bin').write_bytes(b'x' * 4096)
            policy = {'max_file_bytes': 1024, 'max_total_bytes': 1024,
                      'max_large_file_bytes': 8192, 'max_large_total_bytes': 8192}
            with self.assertRaises(ValueError):
                build_snapshot(root, ['model.bin'], policy=policy)
            snapshot = build_snapshot(root, [{'kind': 'large_artifact', 'path': 'model.bin'}], policy=policy)
            self.assertEqual(snapshot['large_total_bytes'], 4096)
            self.assertTrue(validate_snapshot(root, snapshot)['valid'])
            (root / 'model.bin').write_bytes(b'y' * 4096)
            with self.assertRaises(ValueError):
                validate_snapshot(root, snapshot)


class RecoveryTests(unittest.TestCase):
    def test_failure_categories_are_bounded_and_actionable(self):
        from apzn.recovery import record_failure
        state = {}
        first = record_failure(state, 'test_failure', 'Observed command exited 1', 'checks/one.json')
        second = record_failure(state, 'test_failure', 'Same assertion still fails', 'checks/two.json')
        self.assertEqual(first['attempt'], 1)
        self.assertEqual(second['attempt'], 2)
        self.assertIn('assumption', second['next_action'])
        with self.assertRaises(ValueError):
            record_failure(state, 'success', 'fake', 'checks/three.json')

    def test_failure_receipt_is_real_nonzero_and_current(self):
        from apzn.evidence import run_check, validate_execution
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'subject.txt').write_text('input', encoding='utf-8')
            spec = {'run_id':'run-fixture','contract_revision':3,
                    'subject_paths':['subject.txt'],
                    'argv':[sys.executable,'-c','import sys;sys.exit(7)']}
            receipt = run_check(spec, root)['receipt']
            self.assertEqual(validate_execution(root,receipt,run_id='run-fixture',
                             revision=3,expect_failure=True)['result']['exit_code'],7)
            with self.assertRaises(ValueError):
                validate_execution(root,receipt,run_id='run-fixture',revision=3)

    def test_auth_failure_cannot_be_fabricated_and_blocks_retry(self):
        from apzn.native import apply_request, initial
        from apzn.evidence import run_check
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);ws=base/'workspace';ws.mkdir()
            codex=base/'codex';codex.mkdir()
            (ws/'subject.txt').write_text('input',encoding='utf-8')
            with patch.dict(os.environ,{'CODEX_HOME':str(codex),'APZN_EXEC_OWNER':''}):
                state=initial('0'*24,ws);state.update(status='working',run_id='run-auth',intent_version=1)
                state['contract']={'criteria':[],'artifacts':[],'intent_version':1}
                state['contract_hash']=digest(state['contract'])
                ordinary=run_check({'run_id':'run-auth','contract_revision':1,
                    'subject_paths':['subject.txt'],
                    'argv':[sys.executable,'-c','import sys;sys.exit(3)']},ws)['receipt']
                payload={'summary':'Observed failure','next_action':'Inspect auth',
                    'failure':{'category':'auth_required','detail':'Claimed auth error',
                               'evidence_id':ordinary}}
                with self.assertRaisesRegex(ValueError,'differs from verified process output'):
                    apply_request(state,'checkpoint',payload,base)
                real=run_check({'run_id':'run-auth','contract_revision':1,
                    'subject_paths':['subject.txt'],
                    'argv':[sys.executable,'-c',
                            'import sys;sys.stderr.write("authentication required");sys.exit(3)']},ws)['receipt']
                payload['failure']['evidence_id']=real
                apply_request(state,'checkpoint',payload,base)
                self.assertEqual(state['status'],'blocked')
                self.assertFalse(state['failure_history'][-1]['retry_allowed'])
                with self.assertRaisesRegex(ValueError,'No active native run'):
                    apply_request(state,'checkpoint',payload,base)

    def test_repeated_test_failure_requires_changed_plan(self):
        from apzn.native import apply_request, initial
        from apzn.evidence import run_check
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);ws=base/'workspace';ws.mkdir()
            codex=base/'codex';codex.mkdir()
            (ws/'subject.txt').write_text('input',encoding='utf-8')
            plan={'objective':'Fix a failing check','approach':'Inspect input',
                  'verification':'Rerun the check','preflight':['Read input'],
                  'risks':['Wrong assumption'],'alternatives':['Different input'],
                  'steps':[{'id':'S1','action':'Check','depends_on':[],
                            'requirements':['R1']}]}
            with patch.dict(os.environ,{'CODEX_HOME':str(codex),'APZN_EXEC_OWNER':''}):
                state=initial('0'*24,ws);state.update(status='working',run_id='run-retry',intent_version=1)
                state['contract']={'criteria':[{'id':'R1','kind':'test','text':'Pass'}],
                                   'artifacts':['subject.txt'],'intent_version':1}
                state['contract_hash']=digest(state['contract'])
                state['plan']={'content':plan,'sha256':digest(plan),'intent_version':1}
                for index in range(3):
                    ref=run_check({'run_id':'run-retry','contract_revision':1,
                        'subject_paths':['subject.txt'],
                        'argv':[sys.executable,'-c',
                                'import sys;sys.stderr.write("AssertionError");sys.exit(1)']},ws)['receipt']
                    apply_request(state,'checkpoint',{'summary':f'Failure {index}',
                        'next_action':'Revisit hypothesis','failure':{'category':'test_failure',
                        'detail':'Observed assertion','evidence_id':ref}},base)
                self.assertTrue(state['needs_replan'])
                with self.assertRaisesRegex(ValueError,'Change the plan'):
                    apply_request(state,'candidate',{'criterion_results':[]},base)
                with self.assertRaisesRegex(ValueError,'changed plan'):
                    apply_request(state,'plan',plan,base)
                revised={**plan,'approach':'Inspect boundary conditions'}
                apply_request(state,'plan',revised,base)
                self.assertFalse(state['needs_replan'])


class ModelObservationTests(unittest.TestCase):
    def test_unavailable_usage_stays_unmeasured(self):
        from apzn.native import initial, start_child, stop_child
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = initial('0'*24, root)
            state['status'] = 'working'
            state['reservations'] = {'tool-1': {'role':'apzn-scout','time':0,
                'tool_use_id':'tool-1','selection_reason':'Read a bounded source'}}
            start_child(state,{'agent_type':'apzn-scout','agent_id':'agent-1',
                               'tool_use_id':'tool-1','model':'gpt-6-luna'})
            stop_child(state,{'agent_id':'agent-1','last_assistant_message':'Found one file'},root)
            row = state['model_calls'][0]
            self.assertEqual(row['selected_model'],'gpt-6-luna')
            self.assertEqual(row['observed_model'],'gpt-6-luna')
            self.assertFalse(row['usage_observed'])
            self.assertIsNone(row['usage'])


class ProcedureBundleTests(unittest.TestCase):
    def test_bundle_is_recalled_only_in_matching_environment(self):
        from apzn.memory import Memory
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bundle = {'input_conditions': ['A source PDF exists'],
                      'execution_script': ['Render the output'],
                      'verifier': ['Compare every page'],
                      'recovery': ['Rerender after font repair']}
            with Memory(root / 'memory.sqlite3') as memory:
                memory.save_procedure(root, 'report', 'render a report', ['Render all pages'],
                                      'verified completion fixture', 'env-a', bundle=bundle)
                rows = memory.procedure_recipes(root, 'report', 'render a report', 'env-a')
                self.assertEqual(rows[0]['bundle'], bundle)
                self.assertEqual(memory.procedure_recipes(root, 'report', 'render a report', 'env-b'), [])


class LearningModeTests(unittest.TestCase):
    def test_study_needs_self_check_but_production_does_not(self):
        from apzn.gates import validate_learning_check
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'answer.md').write_text('설명과 예제', encoding='utf-8')
            def checked(workspace, relative, existing=False):
                path = workspace / relative
                if existing and not path.is_file():
                    raise ValueError('missing')
                return path
            with self.assertRaises(ValueError):
                validate_learning_check('study', [{'text': '이 개념 공부하고 싶어'}], None, root, checked)
            check = {'question': '새 예제에 적용해 봐', 'answer_key': '개념을 적용한다',
                     'explanation_file': 'answer.md'}
            self.assertEqual(validate_learning_check('study', [{'text': '공부'}], check, root, checked), check)
            self.assertIsNone(validate_learning_check('report', [{'text': '보고서 작성'}], None, root, checked))
            self.assertEqual(validate_learning_check('study', [{'text': '완전한 풀이를 줘'}], None,
                                                     root, checked)['format'], 'full_solution_requested')


class McpSmokeTests(unittest.TestCase):
    def test_probe_calls_only_a_reviewed_read_only_tool(self):
        from apzn import extensions
        calls = []
        class Result:
            isError = False
            def model_dump(self, mode='json'):
                return {'content': [{'type': 'text', 'text': 'pong'}]}
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
            async def initialize(self):
                return SimpleNamespace(serverInfo=SimpleNamespace(model_dump=lambda: {'name': 'fixture'}))
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name='ping',
                    annotations=SimpleNamespace(readOnlyHint=True))])
            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return Result()
        @asynccontextmanager
        async def stdio_client(_params):
            yield object(), object()
        mcp = ModuleType('mcp');mcp.__path__=[]
        mcp.ClientSession=lambda *_: Session()
        mcp.StdioServerParameters=lambda **kwargs: kwargs
        client = ModuleType('mcp.client');client.__path__=[]
        stdio = ModuleType('mcp.client.stdio');stdio.stdio_client=stdio_client
        record = {'id': 'fixture', 'config': str(Path.cwd() / 'config.toml'),
                  'probe_call': {'tool': 'ping', 'arguments': {}, 'read_only': True,
                                 'purpose': 'Check local response'}}
        with (patch.dict(sys.modules, {'mcp':mcp,'mcp.client':client,'mcp.client.stdio':stdio}),
              patch('apzn.extensions._runtime_env', return_value={})):
            result = asyncio.run(extensions._probe_stdio(record))
        self.assertEqual(calls, [('ping', {})])
        self.assertEqual(result['tool_call']['status'], 'succeeded')
        with self.assertRaises(ValueError):
            extensions.validate_probe_call({'tool':'delete','arguments':{},'read_only':False,
                                            'purpose':'unsafe'})
        with self.assertRaises(ValueError):
            extensions.validate_probe_call({'tool':'delete_all','arguments':{},'read_only':True,
                                            'purpose':'unsafe'})


class HostObservationTests(unittest.TestCase):
    def test_only_observed_browser_result_can_satisfy_browser_evidence(self):
        from apzn.evidence import observe_host_tool, validate_tool_observations
        state = {'status':'working','run_id':'run-1','intent_version':2,'tool_observations':[]}
        self.assertIsNone(observe_host_tool(state, {'tool_name':'mcp__cua_repl__js',
                                                    'tool_response':{'content':'clicked'}}))
        row = observe_host_tool(state, {'tool_name':'mcp__cua_repl__js',
             'tool_use_id':'tool-1','tool_input':{'code':'click'},
             'tool_response':{'content':'clicked','isError':False}})
        expected={'name':'mcp__cua_repl__js','request_sha256':digest({'code':'click'}),
                  'result_type':'text','result_contains':'clicked'}
        self.assertEqual(validate_tool_observations(state,[row['id']],'browser',expected),[row])
        with self.assertRaises(ValueError):
            validate_tool_observations(state,['invented'],'browser',expected)
        self.assertIsNone(observe_host_tool(state, {'tool_name':'mcp__fake__read',
            'tool_use_id':'tool-2','tool_response':{'isError':True}}))
        self.assertIsNone(observe_host_tool(state, {'tool_name':'mcp__fake__read',
            'tool_use_id':'tool-3','tool_input':{},
            'tool_response':{'content':[],'isError':False}}))

    def test_unrelated_mcp_result_cannot_satisfy_criterion(self):
        from apzn.evidence import observe_host_tool
        from apzn.native import result_valid
        contract={'name':'mcp__database__read','request_sha256':digest({'table':'items'}),
                  'result_type':'structured'}
        state={'status':'working','run_id':'run-1','intent_version':2,
               'tool_observations':[], 'contract':{'criteria':[
                   {'id':'R1','kind':'mcp','tool_contract':contract}]}}
        unrelated=observe_host_tool(state,{'tool_name':'mcp__unrelated__get_status',
            'tool_use_id':'other','tool_input':{},
            'tool_response':{'content':[{'type':'text','text':'ok'}],'isError':False}})
        with self.assertRaisesRegex(ValueError,'differs from its criterion'):
            result_valid(state,[{'id':'R1','pass':True,'evidence':'Unrelated status',
                                  'observation_ids':[unrelated['id']]}],require_execution=True)

    def test_free_text_cannot_satisfy_browser_or_provenance_gate(self):
        from apzn.native import result_valid
        with tempfile.TemporaryDirectory() as d:
            state={'workspace':d,'run_id':'run-1','intent_version':2,'tool_observations':[],
                   'contract':{'criteria':[{'id':'R1','kind':'browser'},
                                           {'id':'R2','kind':'provenance'}]}}
            rows=[{'id':'R1','pass':True,'evidence':'I clicked it'},
                  {'id':'R2','pass':True,'evidence':'The numbers match'}]
            with self.assertRaises(ValueError):
                result_valid(state,rows,require_execution=True)


class NativeProvenanceGateTests(unittest.TestCase):
    def test_candidate_requires_manifest_and_real_subject_bound_check(self):
        from apzn.native import handle_event, session_key, submit_request, folder
        from apzn.core import read_json
        from apzn.evidence import run_check
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);ws=base/'workspace';ws.mkdir()
            codex=base/'codex';codex.mkdir()
            sid=uuid.uuid4().hex;key=session_key(sid)
            def event(name, turn, **kwargs):
                return handle_event({'hook_event_name':name,'session_id':sid,
                                     'cwd':str(ws),'turn_id':str(turn),**kwargs})
            def request(op,payload,turn):
                queued=submit_request(key,ws,op,payload)
                event('PostToolUse',turn,tool_name='Bash')
                return read_json(Path(queued['receipt']))
            with patch.dict(os.environ,{'CODEX_HOME':str(codex),'APZN_EXEC_OWNER':''}):
                event('UserPromptSubmit',1,prompt='검증된 평균값 보고서를 작성해')
                plan={'objective':'Report one measured result','approach':'Hash and compare',
                      'verification':'Run check and validate manifest','preflight':['Read data'],
                      'risks':['Stale values'],'alternatives':['Manual comparison'],
                      'steps':[{'id':'S1','action':'Compute and check','depends_on':[],
                                'requirements':['R1']}]}
                begin=request('begin',{'mode':'research','criteria':[{'id':'R1',
                    'text':'Numbers agree','kind':'provenance'}],
                    'artifacts':['report.md'],'plan':plan},2)
                self.assertTrue(begin['ok'],begin)
                for name,content in {'raw.csv':'1,2\n','analysis.py':
                    'import json\nfrom pathlib import Path\nraw=[float(x) for x in Path("raw.csv").read_text().strip().split(",")]\nassert json.loads(Path("result.json").read_text())["mean"] == sum(raw)/len(raw)\n',
                    'result.json':'{"mean":1.5}\n','report.md':'Mean 1.5 kg\n'}.items():
                    (ws/name).write_text(content,encoding='utf-8')
                manifest={'schema':1,'nodes':[
                    {'id':'raw','role':'raw','path':'raw.csv','sha256':file_hash(ws/'raw.csv'),'inputs':[]},
                    {'id':'code','role':'analysis','path':'analysis.py','sha256':file_hash(ws/'analysis.py'),'inputs':['raw']},
                    {'id':'result','role':'result','path':'result.json','sha256':file_hash(ws/'result.json'),'inputs':['raw','code']},
                    {'id':'report','role':'report','path':'report.md','sha256':file_hash(ws/'report.md'),'inputs':['result']}],
                    'claims':[{'id':'mean','category':'calculated','result':'result',
                               'json_pointer':'/mean','output':'report','literal':'1.5 kg',
                               'value':'1.5','unit':'kg'}]}
                (ws/'provenance.json').write_text(json.dumps(manifest),encoding='utf-8')
                state=read_json(folder(key)/'state.json')
                check=run_check({'run_id':state['run_id'],'contract_revision':state['intent_version'],
                    'subject_paths':['raw.csv','analysis.py','result.json','report.md'],
                    'argv':[sys.executable,'-c','import sys;sys.exit(0)']},ws)
                candidate={'files':['report.md'],'criterion_results':[{'id':'R1','pass':True,
                    'evidence':'Current numeric data inspected','evidence_ids':[check['receipt']]}]}
                self.assertFalse(request('candidate',candidate,3)['ok'])
                candidate['provenance_manifests']=[{'criterion_id':'R1','manifest':'provenance.json'}]
                self.assertFalse(request('candidate',candidate,4)['ok'])
                non_interpreter=shutil.which('where') or shutil.which('true')
                if non_interpreter:
                    fake=run_check({'run_id':state['run_id'],'contract_revision':state['intent_version'],
                        'subject_paths':['raw.csv','analysis.py','result.json','report.md'],
                        'argv':[non_interpreter,str(ws/'analysis.py')]},ws)
                    if fake['exit_code']==0:
                        candidate['criterion_results'][0]['evidence_ids']=[fake['receipt']]
                        self.assertFalse(request('candidate',candidate,5)['ok'])
                analysis_check=run_check({'run_id':state['run_id'],'contract_revision':state['intent_version'],
                    'subject_paths':['raw.csv','analysis.py','result.json','report.md'],
                    'argv':[sys.executable,str(ws/'analysis.py')]},ws)
                candidate['criterion_results'][0]['evidence_ids']=[analysis_check['receipt']]
                self.assertTrue(request('candidate',candidate,6)['ok'])


if __name__ == '__main__':
    unittest.main()
