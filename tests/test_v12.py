"""v0.1 invariants. Real local files/SQLite/ZIP/PDF; model and MCP tests use doubles."""
from __future__ import annotations
import copy,json,os,sys,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch,AsyncMock
from apzn.core import atomic_json,file_hash,read_json,store
from apzn.memory import Memory,namespace
from apzn import documents as docs,extensions as ext
from apzn.gates import validate_plan,validate_document_contract,review_documents
from apzn.native import checked_path,handle_event,session_key,folder,submit_request
from apzn.skill_router import route_skills,configure_router
from apzn.wiki import import_zip,scan_zip
from apzn.native_install import install_native,upgrade_native,rollback_native

PLAN={'objective':'Satisfy exact original requirements','approach':'Small change with baseline checks',
 'verification':'Run original behavior and regression tests','preflight':['Check input and dependencies'],
 'risks':['Potential regression; preserve input'],'alternatives':['Minimal patch rather than full rewrite'],
 'steps':[{'id':'S1','action':'Inspect then implement and verify','requirements':['R1'],'depends_on':[]}]}

class Isolated(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.ws=self.base/'project';self.ws.mkdir()
  self.ch=self.base/'codex';self.ch.mkdir();self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.ch),'APZN_HOME':str(self.ch/'apzn'),'APZN_EXEC_OWNER':''});self.env.start()
 def tearDown(self):self.env.stop();self.temp.cleanup()

class MemoryTests(Isolated):
 def setUp(self):super().setUp();self.m=Memory();self.scope=namespace(self.ws)
 def tearDown(self):self.m.close();super().tearDown()
 def put(self,**kw):
  v={'scope':self.scope,'kind':'fact','key':'format','content':'보고서 글자 크기 10pt','source':'requirements.md','locator':'L1','basis':'source_read'};v.update(kw);return self.m.put(**v)
 def test_insert_recall(self):
  self.put();self.assertEqual(len(self.m.recall('글자 크기',self.scope)['records']),1)
 def test_scope_isolation(self):
  self.put(scope=namespace(self.base/'other'));self.assertEqual(self.m.recall('보고서',self.scope)['records'],[])
 def test_global_opt_out(self):
  self.put(scope='global');self.assertEqual(self.m.recall('보고서',self.scope,include_global=False)['records'],[])
 def test_duplicate_is_idempotent(self):self.assertEqual(self.put()['id'],self.put()['id'])
 def test_conflict_excludes_both(self):
  self.put();self.put(content='보고서 글자 크기 12pt');self.assertEqual(self.m.recall('보고서',self.scope)['records'],[])
 def test_explicit_conflict_resolution(self):
  self.put();b=self.put(content='보고서 글자 크기 12pt');self.m.resolve(b['id'],evidence='Current user template L4');self.assertEqual(self.m.recall('글자',self.scope)['records'][0]['content'],'보고서 글자 크기 12pt')
 def test_resolution_requires_evidence(self):
  b=self.put()
  with self.assertRaises(ValueError):self.m.resolve(b['id'],evidence='')
 def test_forget_resurrection_blocked(self):
  b=self.put();self.m.forget(b['id']);self.assertEqual(self.put()['status'],'forgotten');self.assertEqual(self.m.recall('보고서',self.scope)['records'],[])
 def test_ttl_expires(self):
  self.put(observed=time.time()-2*86400,ttl_days=1);self.assertEqual(self.m.recall('보고서',self.scope)['records'],[])
 def test_expired_fact_does_not_poison_new_observation(self):
  self.put(observed=time.time()-2*86400,ttl_days=1);self.assertEqual(self.put(content="보고서 최신 조건") ["status"],"active")
 def test_future_observation_rejected(self):
  with self.assertRaises(ValueError):self.put(observed=time.time()+9999)
 def test_secret_rejected(self):
  with self.assertRaises(ValueError):self.put(content='api_key=12345678901234567890')
 def test_procedure_requires_receipt(self):
  with self.assertRaises(ValueError):self.put(kind='procedure')
 def test_verified_fact_requires_receipt(self):
  with self.assertRaises(ValueError):self.put(basis='verified_run')
 def test_environment_mismatch(self):
  self.put(environment='old-lockhash');self.assertEqual(self.m.recall('보고서',self.scope,environment='new-lockhash')['records'],[])
 def test_bounded_retrieval(self):
  for n in range(10):self.put(key=str(n),content='보고서 '+('길이 '*200))
  result=self.m.recall('보고서',self.scope,limit=2,max_chars=1000);self.assertLessEqual(len(result['records']),2)
 def test_invalid_scope(self):
  with self.assertRaises(ValueError):self.put(scope='other-user')
 def test_raw_source_pointer_required(self):
  with self.assertRaises(ValueError):self.put(locator='')
 def test_reopen_persists(self):
  self.put();self.m.close();self.m=Memory();self.assertEqual(len(self.m.recall('글자',self.scope)['records']),1)

class DocumentTests(Isolated):
 def make_zip(self,kind='docx'):
  p=self.ws/('in.'+kind)
  if kind=='docx':part='word/document.xml';xml=b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>before</w:t></w:r></w:p></w:body></w:document>'
  elif kind=='pptx':part='ppt/slides/slide1.xml';xml=b'<p:sld xmlns:p="urn:test" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>before</a:t></p:sld>'
  else:part='Contents/section0.xml';xml=b'<hp:sec xmlns:hp="urn:test"><hp:p><hp:run><hp:t>before</hp:t></hp:run></hp:p></hp:sec>'
  with zipfile.ZipFile(p,'w') as z:
   z.writestr(part,xml);z.writestr('unchanged.bin',b'original')
   if kind=='pptx':z.writestr('ppt/presentation.xml','<presentation/>')
   if kind=='hwpx':z.writestr('mimetype','application/hwp+zip')
  return p,docs.inspect_document(p)['text_nodes'][0]
 def edit(self,kind):
  p,node=self.make_zip(kind);old=file_hash(p);out=self.ws/('edited.'+kind)
  result=docs.edit_text(p,out,[{'part':node['part'],'node':node['node'],'before':'before','after':'after'}])
  self.assertEqual(file_hash(p),old);self.assertEqual(docs.inspect_document(out)['text'],'after');self.assertFalse(result['visual_verified']);self.assertEqual(docs.zip_parts(out)['unchanged.bin'],b'original')
 def test_docx_exact_edit(self):self.edit('docx')
 def test_pptx_native_text_edit(self):self.edit('pptx')
 def test_hwpx_xml_fixture_edit(self):self.edit('hwpx')
 def test_wrong_anchor_no_output(self):
  p,n=self.make_zip();out=self.ws/'edited.docx'
  with self.assertRaises(ValueError):docs.edit_text(p,out,[{'part':n['part'],'node':n['node'],'before':'wrong','after':'x'}])
  self.assertFalse(out.exists())
 def test_no_overwrite_source(self):
  p,n=self.make_zip()
  with self.assertRaises(ValueError):docs.edit_text(p,p,[])
 def test_no_extension_renaming(self):
  p,n=self.make_zip()
  with self.assertRaises(ValueError):docs.edit_text(p,self.ws/'fake.hwp',[])
 def test_zip_traversal(self):
  p=self.ws/'bad.docx'
  with zipfile.ZipFile(p,'w') as z:z.writestr('../escape',b'x')
  with self.assertRaises(ValueError):docs.zip_parts(p)
 def test_external_entities_rejected(self):
  with self.assertRaises(ValueError):docs._xml(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')
 def test_render_real_pdf_and_tamper(self):
  import fitz
  p=self.ws/'original.pdf';d=fitz.open();d.new_page().insert_text((40,40),'Evidence page');d.save(p);d.close()
  m=docs.render_document(p,self.ws/'render');docs.validate_render(m,p,max_pages=1);self.assertFalse(m['visual_verified'])
  Path(m['pages'][0]['image']).write_bytes(b'tampered')
  with self.assertRaises(ValueError):docs.validate_render(m,p)
 def test_page_limit_real_pdf(self):
  import fitz
  p=self.ws/'original.pdf';d=fitz.open();d.new_page();d.new_page();d.save(p);d.close();m=docs.render_document(p,self.ws/'render')
  with self.assertRaises(ValueError):docs.validate_render(m,p,max_pages=1)
 def test_no_stale_render_dir(self):
  p,_=self.make_zip();out=self.ws/'render';out.mkdir()
  with self.assertRaises(ValueError):docs.render_document(p,out)
 def test_hwpx_requires_authoritative_renderer(self):
  p,_=self.make_zip('hwpx')
  with self.assertRaises(ValueError):docs.render_document(p,self.ws/'render')
 def test_unknown_cannot_be_called_hwp(self):
  p=self.ws/'not.hwp';p.write_text('plain text')
  with self.assertRaises(ValueError):docs.edit_binary_hwp(p,self.ws/'out.hwp',[{'paragraph':0,'before':'a','after':'b'}])
 def test_document_review_all_pages_required(self):
  state={'candidate':{'document_evidence':[{'path':'x.pdf','render_sha256':'hash','pages':[1,2]}]}}
  with self.assertRaises(ValueError):review_documents(state,{'document_reviews':[{'path':'x.pdf','render_sha256':'hash','inspected_pages':[1],'pass':True,'evidence':'seen'}]})
 def test_document_review_exact_hash(self):
  state={'candidate':{'document_evidence':[{'path':'x.pdf','render_sha256':'hash','pages':[1]}]}}
  review_documents(state,{'document_reviews':[{'path':'x.pdf','render_sha256':'hash','inspected_pages':[1],'pass':True,'evidence':'Opened every page image; no overflow'}]})

class PlanTests(Isolated):
 def test_valid_plan(self):self.assertEqual(validate_plan(copy.deepcopy(PLAN),[{'id':'R1'}]),PLAN)
 def test_missing_plan(self):
  with self.assertRaises(ValueError):validate_plan(None,[{'id':'R1'}])
 def test_missing_risk_analysis(self):
  p=copy.deepcopy(PLAN);p['risks']=[]
  with self.assertRaises(ValueError):validate_plan(p,[{'id':'R1'}])
 def test_uncovered_requirement(self):
  with self.assertRaises(ValueError):validate_plan(PLAN,[{'id':'R1'},{'id':'R2'}])
 def test_cyclic_dependency(self):
  p=copy.deepcopy(PLAN);p['steps'][0]['depends_on']=['S1']
  with self.assertRaises(ValueError):validate_plan(p,[{'id':'R1'}])
 def test_document_contract_conflicting_limits(self):
  with self.assertRaises(ValueError):validate_document_contract([{'path':'a.pdf','exact_pages':3,'max_pages':2}],['a.pdf'],self.ws,checked_path)
 def test_document_contract_wrong_boolean(self):
  with self.assertRaises(ValueError):validate_document_contract([{'path':'a.pdf','editable_required':'yes'}],['a.pdf'],self.ws,checked_path)
 def test_native_candidate_blocked_without_plan(self):
  sid='v12plan';key=session_key(sid)
  def ev(name,**kwargs):return handle_event({'hook_event_name':name,'session_id':sid,'cwd':str(self.ws),'model':'gpt-6-sol',**kwargs})
  ev('UserPromptSubmit',prompt='Implement a feature and test it')
  req=submit_request(key,self.ws,'begin',{'mode':'coding','artifacts':['a.txt'],'criteria':[{'id':'R1','text':'Correct','kind':'behavior'}]});ev('PostToolUse',tool_name='Bash');self.assertTrue(read_json(Path(req['receipt']))['ok'])
  (self.ws/'a.txt').write_text('file');r=submit_request(key,self.ws,'candidate',{'files':['a.txt'],'criterion_results':[{'id':'R1','pass':True,'evidence':'x'}]});ev('PostToolUse',tool_name='Bash');self.assertFalse(read_json(Path(r['receipt']))['ok'])
  denied=ev('PreToolUse',tool_name='spawn_agent',tool_input={'agent_type':'apzn-implementer','message':'Implement'})
  self.assertEqual(denied['hookSpecificOutput']['permissionDecision'],'deny')

class ExtensionTests(Isolated):
 def skill(self,eid='test-skill'):
  p=self.base/eid;p.mkdir();(p/'SKILL.md').write_text('---\nname: '+eid+'\ndescription: A focused helper\n---\nDo only the requested task.');return {'id':eid,'kind':'skill','source':'local-reviewed-test@1 MIT','local_path':str(p)}
 def mcp(self,eid='test-mcp',**kw):
  v={'id':eid,'kind':'mcp-stdio','source':'local-test@1','command':str(Path(sys.executable).resolve()),'args':[]};v.update(kw);return v
 def test_stage_requires_source(self):
  m=self.skill();m['source']=''
  with self.assertRaises(ValueError):ext.stage(m)
 def test_skill_review_required(self):
  ext.stage(self.skill())
  with self.assertRaises(ValueError):ext.apply('test-skill',agents_home=self.base/'agents')
 def test_skill_install_rollback(self):
  ext.stage(self.skill());r=ext.apply('test-skill',reviewed=True,agents_home=self.base/'agents');self.assertFalse(r['runtime_verified']);self.assertTrue(Path(r['target']).is_dir());ext.rollback('test-skill');self.assertFalse(Path(r['target']).exists())
 def test_staged_mutation_rejected(self):
  ext.stage(self.skill());(store()/'extensions/test-skill/payload/SKILL.md').write_text('changed')
  with self.assertRaises(ValueError):ext.apply('test-skill',reviewed=True,agents_home=self.base/'agents')
 def test_rollback_preserves_user_change(self):
  ext.stage(self.skill());r=ext.apply('test-skill',reviewed=True,agents_home=self.base/'agents');(Path(r['target'])/'SKILL.md').write_text('user edit')
  with self.assertRaises(ValueError):ext.rollback('test-skill')
 def test_mcp_disabled_until_probe(self):
  ext.stage(self.mcp());r=ext.apply('test-mcp',reviewed=True,codex_home=self.ch);self.assertFalse(r['entry']['enabled']);self.assertFalse(r['runtime_verified'])
 def test_mcp_auth_missing_not_success(self):
  ext.stage(self.mcp(env_vars=['APZN_UNSET_TEST_AUTH']));ext.apply('test-mcp',reviewed=True,codex_home=self.ch);self.assertEqual(ext.probe('test-mcp')['status'],'auth_required')
 def test_mock_handshake_without_real_call_stays_disabled(self):
  ext.stage(self.mcp());ext.apply('test-mcp',reviewed=True,codex_home=self.ch)
  with patch('apzn.extensions.importlib.util.find_spec',return_value=object()),patch('apzn.extensions._probe_stdio',new=AsyncMock(return_value={'server':{'name':'mock'},'tools':['read_test']})):
   r=ext.probe('test-mcp');self.assertFalse(r['entry']['enabled']);self.assertEqual(r['status'],'tool_call_required')
 def test_mcp_no_inline_eval(self):
  with self.assertRaises(ValueError):ext.stage(self.mcp(args=['-c','print(1)']))
 def test_mcp_secret_manifest_rejected(self):
  with self.assertRaises(ValueError):ext.stage(self.mcp(args=['api_key=12345678901234567890']))
 def test_mcp_revert_preserves_other_server(self):
  (self.ch/'config.toml').write_text('# comment\n[mcp_servers.other]\ncommand="keep"\n');ext.stage(self.mcp());ext.apply('test-mcp',reviewed=True,codex_home=self.ch);ext.rollback('test-mcp');s=(self.ch/'config.toml').read_text();self.assertIn('other',s);self.assertNotIn('test-mcp',s)

class WikiRouterTests(Isolated):
 def zip(self):
  p=self.base/'wiki.zip'
  with zipfile.ZipFile(p,'w') as z:
   z.writestr('wiki/20 지식/계획.md','# 계획\n출처를 확인한다.');z.writestr('wiki/사용자 프로필.md','private');z.writestr('wiki/10 기록/대화.md','private conversation');z.writestr('wiki/.git/objects/x',b'binary');z.writestr('wiki/scripts/skill_wiki.py','raise RuntimeError("do not run")')
  return p
 def test_inventory_does_not_execute(self):r=scan_zip(self.zip());self.assertFalse(r['executed']);self.assertFalse(r['extracted'])
 def test_default_import_excludes_private_history(self):
  r=import_zip(self.zip(),self.ws);self.assertEqual(r['selected'],1);self.assertFalse(r['private_history_included'])
 def test_reimport_is_idempotent(self):
  p=self.zip();a=import_zip(p,self.ws);b=import_zip(p,self.ws);self.assertEqual(a['records'][0]['id'],b['records'][0]['id'])
 def test_route_only_small_catalog(self):
  entries=[{'name':'문서'+str(n),'description':'보고서 편집','path':'x'} for n in range(10)]
  with patch('apzn.skill_router.discover_skills',return_value=entries):
   r=route_skills('보고서 편집');self.assertLessEqual(len(r['primary'])+len(r['supporting']),4)
 def test_router_change_requires_review(self):
  p=self.base/'skill_wiki.py';p.write_text('print("{}")');configure_router(p);p.write_text('print("changed")')
  with self.assertRaises(ValueError):route_skills('보고서')
 def test_upgrade_new_install_and_refs(self):
  from apzn import __version__
  r=upgrade_native(self.ch,self.base/'agents');self.assertEqual(r['version'],__version__);self.assertTrue((self.base/'agents/skills/apzn-native/references/v12-gates.md').is_file());rollback_native(self.ch)

class NativeDocumentFlowTests(Isolated):
 def setUp(self):
  super().setUp();self.sid='native-document-flow';self.key=session_key(self.sid);self.turn=0
  self.event('UserPromptSubmit',prompt='Create a one-page PDF, preserve original input and inspect every page')
 def event(self,name,**kwargs):
  self.turn+=1;return handle_event({'hook_event_name':name,'session_id':self.sid,'cwd':str(self.ws),'model':'gpt-6-sol','turn_id':str(self.turn),**kwargs})
 def req(self,op,data):
  q=submit_request(self.key,self.ws,op,data);self.event('PostToolUse',tool_name='Bash');return read_json(Path(q['receipt']))
 def begin(self):
  (self.ws/'original.txt').write_text('Do not change this original')
  data={'mode':'report','criteria':[{'id':'R1','text':'One readable page','kind':'visual'}],'artifacts':['final.pdf'],'documents':[{'path':'final.pdf','exact_pages':1}],'protected_inputs':['original.txt'],'plan':copy.deepcopy(PLAN)}
  self.assertTrue(self.req('begin',data)['ok'])
  import fitz
  d=fitz.open();d.new_page().insert_text((40,40),'Final evidence');d.save(self.ws/'final.pdf');d.close();docs.render_document(self.ws/'final.pdf',self.ws/'render')
 def candidate(self,render=True):
  data={'files':['final.pdf'],'criterion_results':[{'id':'R1','pass':True,'evidence':'One-page PDF and current render inspected'}],'procedure':['Render and inspect final PDF']}
  if render:data['document_evidence']=[{'path':'final.pdf','render_manifest':'render/render.json'}]
  return self.req('candidate',data)
 def test_native_pdf_requires_render(self):self.begin();self.assertFalse(self.candidate(False)['ok'])
 def test_native_protected_original_blocks_candidate(self):
  self.begin();(self.ws/'original.txt').write_text('changed');self.assertFalse(self.candidate()['ok'])
 def test_native_pdf_observed_reviewer_completes(self):
  self.begin();self.assertTrue(self.candidate()['ok']);self.event('PreToolUse',tool_name='spawn_agent',tool_use_id='document-review-tool',tool_input={'agent_type':'apzn-verifier','message':'Review final PDF and original input'})
  self.event('SubagentStart',agent_type='apzn-verifier',agent_id='observed-reviewer',tool_use_id='document-review-tool',model='gpt-6-sol')
  s=read_json(folder(self.key)/'state.json');c=s['candidate'];e=c['document_evidence'][0]
  verdict={'verdict':'approve','reviewed_digest':c['digest'],'reviewed_contract_hash':s['contract_hash'],
   'reviewed_turn_ids':[p['turn_id'] for p in s['prompts']],'intent_alignment':True,
   'criterion_results':c['criterion_results'],'findings':[],
   'document_reviews':[{'path':e['path'],'render_sha256':e['render_sha256'],'inspected_pages':[1],'pass':True,'evidence':'Test double: explicit complete visual review record'}]}
  self.event('SubagentStop',agent_type='apzn-verifier',agent_id='observed-reviewer',last_assistant_message=json.dumps(verdict))
  self.assertEqual(read_json(folder(self.key)/'state.json')['status'],'complete')
  (self.ws/'original.txt').write_text('late mutation');self.assertEqual(self.event('Stop')['decision'],'block')
