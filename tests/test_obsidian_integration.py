from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from adhd import core, experience, experience_storage as storage, obsidian
from adhd.evidence import run_check, validate_execution
from adhd.memory import Memory, namespace
from adhd.native_install import _release_files
from adhd.obsidian_assets import bridge
from adhd.snapshots import build_snapshot

ROOT=Path(__file__).resolve().parents[1]
ENGINE=Path(os.environ.get('ADHD_TEST_WIKI_CONTEXT',str(Path.home()/'Documents/wiki/99 시스템/도구/wiki_context.py')))


@unittest.skipUnless(ENGINE.is_file(),'Existing wiki engine is unavailable; no substitute engine is claimed')
class LiveEngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.ws=self.base/'workspace';self.ws.mkdir()
        self.vault=self.base/'vault';self.vault.mkdir()
        tool=self.vault/'99 시스템/도구/wiki_context.py';tool.parent.mkdir(parents=True);shutil.copyfile(ENGINE,tool)
        self.environment=patch.dict(os.environ,{'ADHD_HOME':str(self.base/'host'),'CODEX_HOME':str(self.base/'codex')})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.configure()
        for relative in ['10 기록','20 지식','99 시스템/템플릿/ADHD','99 시스템/베이스']:
            (self.vault/relative).mkdir(parents=True,exist_ok=True)
        (self.vault/'AGENTS.md').write_text('Fixture entrypoint',encoding='utf-8')
        operating=self.vault/'99 시스템/운영/Codex 컨텍스트.md';operating.parent.mkdir(parents=True)
        operating.write_text('Fixture entrypoint',encoding='utf-8')

    def configure(self,**overrides):
        return obsidian.configure(self.ws,{'vault':str(self.vault),'enabled':True,'engine':'99 시스템/도구/wiki_context.py',
            'read_roots':['10 기록','20 지식'],'allowed_privacy':['public','private'],
            'write_enabled':True,'write_roots':['10 기록','20 지식','99 시스템/템플릿/ADHD','99 시스템/베이스'],**overrides})

    def note(self,name,identity,body,**extra):
        path=self.vault/'10 기록'/name;path.parent.mkdir(parents=True,exist_ok=True)
        fields={'id':identity,'type':'artifact','domain':'artifact','layer':'record','privacy':'public','status':'inbox',
            'template':False,'source_ids':['test:actual-engine'],**extra}
        path.write_bytes(bridge._flat(fields,body));return path

    def test_large_section_revision_and_citation_identity(self):
        self.note('incident.md','incident:one','# Incident\n## Proof\n'+'evidence '*2000)
        self.note('decision.md','decision:one','# Decision',source_ids=['incident:one'])
        packet=bridge.read_note(self.ws,'incident:one',section='Proof',max_chars=2500)
        self.assertEqual(len(packet['cards']),1);self.assertTrue(packet['cards'][0]['id'].endswith(':incident:one'))
        self.assertLessEqual(len(json.dumps(packet,ensure_ascii=False)),2500)
        self.assertIn('excerpt-truncated',packet['warnings'])
        self.assertIn('stale-revision',bridge.read_note(self.ws,'incident:one',revision='0'*64)['warnings'])
        link=bridge.open_link(self.ws,'incident:one','Proof');self.assertFalse(link['launch_performed'])
        self.assertIn('obsidian://open?',link['uri'])

    def test_forgetting_source_does_not_forget_note_that_cites_it(self):
        self.note('incident.md','incident:one','# Incident\nneedle')
        self.note('decision.md','decision:one','# Decision\nDifferent observation',source_ids=['incident:one'])
        result=bridge.forget_note(self.ws,'incident:one');self.assertEqual(result['status'],'forgotten')
        self.assertEqual(bridge.read_note(self.ws,'incident:one')['cards'],[])
        self.assertEqual(len(bridge.read_note(self.ws,'decision:one')['cards']),1)
        self.assertTrue((self.vault/'10 기록/incident.md').exists())

    def test_feedback_candidate_apply_real_parser_and_idempotency(self):
        event={'task_id':'actual-test-task','artifact_revision':'revision-1','kind':'feedback','project':'workspace',
            'title':'Explicit mixed feedback','summary':'Requirements and expression are assessed independently',
            'feedback':[{'polarity':'positive','dimension':'expression','quote':'설명이 간결했다','source':'turn-1','scope':'task'},
                        {'polarity':'negative','dimension':'requirements','quote':'요구한 기능이 빠졌다','source':'turn-1','scope':'project'}]}
        first=experience.candidate(self.ws,event);self.assertEqual(first['status'],'selected')
        self.assertTrue(experience.candidate(self.ws,event)['duplicate'])
        self.assertEqual(len(experience.list_candidates(self.ws)),1)
        applied=experience.apply_candidate(self.ws,first['candidate_id']);self.assertEqual(applied['status'],'applied')
        self.assertEqual(experience.apply_candidate(self.ws,first['candidate_id'])['status'],'already_applied')
        engine=obsidian._engine(obsidian.load_config(self.ws));collection=engine.collect_notes(self.vault)
        records=[note for note in collection.notes if note.metadata.get('type')=='artifact']
        self.assertEqual(len(records),1)
        note=records[0];self.assertTrue(engine.list_values(note.metadata,'source_ids'))
        self.assertIn('설명이 간결했다',note.body);self.assertIn('요구한 기능이 빠졌다',note.body)
        self.assertIn('unverified',note.body)
        self.assertEqual(engine.doctor_payload(self.vault,collection)['status'],'pass')
        self.assertEqual(len(obsidian.context(self.ws,'Explicit mixed feedback')['cards']),1)

    def test_provision_preserves_existing_id_and_exact_rollback(self):
        original=self.note('프로젝트/개인 위키.md','project:personal-wiki','# 개인 위키\n기존 작업 이력',
            type='project',domain='project',source_kind='repo',source_uri=self.ws.as_uri(),projects=['personal-wiki'])
        before=original.read_bytes();preview=bridge.provision(self.ws,{})
        self.assertEqual(preview['status'],'preview');self.assertFalse((self.vault/'10 기록/프로젝트/ADHD.md').exists())
        result=bridge.provision(self.ws,{},apply=True)
        self.assertTrue(original.read_bytes().startswith(before));self.assertIn(b'project:personal-wiki',original.read_bytes())
        engine=obsidian._engine(obsidian.load_config(self.ws));doctor=engine.doctor_payload(self.vault,engine.collect_notes(self.vault))
        self.assertEqual(doctor['errors'],[])
        self.assertEqual(bridge.provision(self.ws,{})['changes'],[])
        restored=bridge.restore_templates(self.ws,{'manifest':result['manifest']})
        self.assertGreater(restored['restored'],0);self.assertEqual(original.read_bytes(),before)
        self.assertFalse((self.vault/'10 기록/프로젝트/ADHD.md').exists())

    def test_provision_rejects_changed_revision_and_user_edit_on_rollback(self):
        with self.assertRaisesRegex(ValueError,'revision conflict'):
            bridge.provision(self.ws,{'expected_revisions':{'10 기록/프로젝트/ADHD.md':'0'*64}},apply=True)
        result=bridge.provision(self.ws,{},apply=True)
        target=self.vault/'10 기록/프로젝트/ADHD.md';target.write_bytes(target.read_bytes()+b'\nuser edit')
        with self.assertRaisesRegex(ValueError,'User edit'):bridge.restore_templates(self.ws,{'manifest':result['manifest']})
        self.assertTrue(target.read_bytes().endswith(b'user edit'))

    def test_combined_recall_deduplicates_only_observed_revision_and_respects_forget(self):
        self.note('proof.md','proof:one','# ProofNeedle\nObserved material')
        card=bridge.read_note(self.ws,'proof:one')['cards'][0]
        with Memory(self.base/'memory.sqlite3') as memory:
            scope=namespace(self.ws)
            self.assertEqual(len(memory.recall_with_wiki('ProofNeedle',scope,workspace=self.ws)['wiki_context']['cards']),1)
            record=memory.observe(observation_id='obs-1',evidence_digest='digest-1',source_revision=card['revision'],
                scope=scope,kind='fact',key=card['id'],content='ProofNeedle observed material',source='Obsidian',locator='proof:one')
            memory.add_alias('obsidian',card['id'],record['id'])
            merged=memory.recall_with_wiki('ProofNeedle',scope,workspace=self.ws)
            self.assertEqual(merged['wiki_context']['cards'],[]);self.assertEqual(len(merged['records']),1)
            memory.forget(record['id'])
            self.assertEqual(memory.recall_with_wiki('ProofNeedle',scope,workspace=self.ws)['wiki_context']['cards'],[])
            self.configure(enabled=False)
            self.assertEqual(memory.recall_with_wiki('ProofNeedle',scope,workspace=self.ws),memory.recall('ProofNeedle',scope))

    def test_native_begin_context_is_data_and_invalid_config_is_fail_closed(self):
        self.note('workspace.md','project:workspace','# workspace',type='project',domain='project',projects=['workspace'],
            source_kind='repo',source_uri=self.ws.as_uri())
        self.note('proof.md','proof:one','# ProofNeedle',projects=['workspace'])
        result=core.prepare_wiki_context(self.ws,'ProofNeedle');self.assertGreater(result['cards'],0)
        packet=json.loads((self.ws/'.adhd/wiki-plan-context.json').read_text())
        self.assertTrue(packet['revalidate_revisions_before_dependent_action'])
        self.assertTrue(all(card['authority']=='source_data' for card in packet['cards']))
        (self.ws/'.adhd/obsidian.json').write_text('{bad',encoding='utf-8')
        result=core.prepare_wiki_context(self.ws,'ProofNeedle');self.assertEqual(result['cards'],0)
        self.assertEqual(json.loads((self.ws/'.adhd/wiki-plan-context.json').read_text())['cards'],[])

    def test_completion_defers_until_published_controller_state(self):
        key='a'*24;run_id=uuid.uuid4().hex;directory=core.store()/'native'/key
        receipt=directory/('completion-'+run_id+'.json');core.atomic_json(receipt,{'pending':True})
        core.atomic_json(directory/'state.json',{'workspace':str(self.ws),'run_id':run_id,'status':'working'})
        self.assertEqual(core.capture_wiki_completion(self.ws,str(receipt))['status'],'deferred')
        self.assertEqual(experience.list_candidates(self.ws),[])
        core.atomic_json(directory/'state.json',{'workspace':str(self.ws),'run_id':run_id,'status':'complete'})
        self.assertEqual(core.capture_wiki_completion(self.ws,str(receipt))['status'],'unverified')
        self.assertEqual(experience.list_candidates(self.ws),[])

    def test_published_completion_records_verified_receipt_and_is_idempotent(self):
        from tests.test_experience_procedures import ProcedureLifecycleTests
        fixture=ProcedureLifecycleTests()
        fixture.workspace=self.ws
        fixture.steps=['Validate the fixture artifact']
        fixture.artifact=self.ws/'artifact.md'
        fixture.artifact.write_text('verified fixture output',encoding='utf-8')
        reference,_=fixture.completion(replay=False)
        receipt=core.store()/'native'/reference['session_key']/('completion-'+reference['run_id']+'.json')
        result=core.capture_wiki_completion(self.ws,str(receipt))
        self.assertEqual(result['status'],'selected')
        records=experience.list_candidates(self.ws)
        self.assertEqual(len(records),1)
        ledger=json.loads((self.ws/'.adhd/experience/ledger.json').read_text(encoding='utf-8'))
        record=ledger['candidates'][result['candidate_id']]['record']
        self.assertEqual(record['technical_status'],'passed')
        self.assertEqual(record['receipts'][0]['kind'],'receipt')
        self.assertEqual(record['receipts'][0]['run_id'],reference['run_id'])
        repeated=core.capture_wiki_completion(self.ws,str(receipt))
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(repeated['candidate_id'],result['candidate_id'])
        self.assertEqual(len(experience.list_candidates(self.ws)),1)

    def test_inventory_only_counts_current_policy_visible_vault_notes(self):
        self.note('public.md','public:one','# Visible')
        self.note('private.md','private:one','# Hidden',privacy='private')
        self.configure(allowed_privacy=['public'])
        inventory=bridge.storage_inventory(self.ws)
        self.assertEqual(inventory['categories']['canonical_notes']['files'],1)
        self.assertIsNone(inventory['reclaimed_disk_bytes']);self.assertFalse(inventory['deletion_performed'])


class ArchiveBackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.ws=self.base/'workspace';self.ws.mkdir()
        environment=patch.dict(os.environ,{'ADHD_HOME':str(self.base/'host')});environment.start();self.addCleanup(environment.stop)
        self.run_id=uuid.uuid4().hex;self.key='b'*24;self.artifact=self.ws/'artifact.txt';self.artifact.write_text('canonical')
        check=run_check({'run_id':self.run_id,'contract_revision':1,'subject_paths':['artifact.txt'],
            'argv':[sys.executable,'-c','import sys; print("repeated observation "*10000); print("stderr",file=sys.stderr)']},self.ws)
        self.receipt=check['receipt'];self.raw=(self.ws/self.receipt).parent/'check.stdout.jsonl';self.original=self.raw.read_bytes()
        self.receipt_hash=core.file_hash(self.ws/self.receipt)
        contract={'intent_version':1,'criteria':[{'id':'R1','kind':'test','text':'byte-exact fixture'}],
            'artifacts':['artifact.txt'],'assumptions':[],'non_goals':[],'documents':[],'protected_inputs':{}}
        candidate={'snapshot_schema':1,'files':build_snapshot(self.ws,['artifact.txt']),'evidence_schema':1,
            'intent_version':1,'contract_hash':core.digest(contract),'execution_receipts':{self.receipt:self.receipt_hash},
            'criterion_results':[{'id':'R1','evidence_ids':[self.receipt]}]}
        candidate['digest']=core.digest(candidate)
        self.state={'schema_version':2,'key':self.key,'workspace':str(self.ws),'run_id':self.run_id,'status':'complete',
            'children':{},'pending_turn_ids':[],'intent_version':1,'contract_hash':core.digest(contract),'contract':contract,
            'tool_observations':[],'candidate':candidate}
        self.state_path=core.store()/'native'/self.key/'state.json';core.atomic_json(self.state_path,self.state)

    def test_actual_archive_release_restore_preserves_bytes_and_receipt(self):
        archived=storage.archive(self.ws,{'run_id':self.run_id,'receipts':[self.receipt]})
        self.assertTrue(archived['originals_retained']);self.assertTrue(self.raw.exists())
        released=storage.archive(self.ws,{'run_id':self.run_id,'receipts':[self.receipt],'release':True})
        self.assertGreater(released['release']['released'],0);self.assertFalse(self.raw.exists())
        self.assertIsNone(released['reclaimed_disk_bytes'])
        self.assertTrue(storage.restore(self.ws,{'manifest':archived['manifest']})['verified'])
        self.assertEqual(self.raw.read_bytes(),self.original);self.assertEqual(core.file_hash(self.ws/self.receipt),self.receipt_hash)
        validate_execution(self.ws,self.receipt,run_id=self.run_id,revision=1)

    def test_actual_archive_rejects_active_owner_and_tampered_archive(self):
        self.state['status']='working';core.atomic_json(self.state_path,self.state)
        with self.assertRaises(ValueError):storage.archive(self.ws,{'run_id':self.run_id,'receipts':[self.receipt],'release':True})
        self.state['status']='complete';core.atomic_json(self.state_path,self.state)
        archived=storage.archive(self.ws,{'run_id':self.run_id,'receipts':[self.receipt]})
        manifest=json.loads((self.ws/archived['manifest']).read_text())
        archive=self.ws/manifest['receipts'][0]['logs'][0]['stored_path'];archive.write_bytes(archive.read_bytes()+b'tampered')
        with self.assertRaises(ValueError):storage.restore(self.ws,{'manifest':archived['manifest']})
        self.assertEqual(self.raw.read_bytes(),self.original)


class PackagingTests(unittest.TestCase):
    def test_fixed_diagnostic_fixture_and_assets_are_packaged(self):
        fixture=json.loads((ROOT/'tests/fixtures/obsidian/evaluation.json').read_text(encoding='utf-8'))
        self.assertEqual(len(fixture['cases']),36);self.assertEqual(sum(c['split']=='dev' for c in fixture['cases']),24)
        self.assertEqual(sum(c['split']=='holdout' for c in fixture['cases']),12)
        self.assertEqual(len({c['category'] for c in fixture['cases']}),6)
        paths={path.relative_to(ROOT).as_posix() for path in _release_files(ROOT)}
        for relative in ['adhd/obsidian_assets/bridge.py','adhd/obsidian_assets/evidence/evidence_archive.py',
            'adhd/obsidian_assets/templates/common.md','adhd/obsidian_assets/views/ADHD 검토.base','schemas/wiki-packet.json']:
            self.assertIn(relative,paths)
        for directory in ['templates','views']:
            for path in (ROOT/'integrations/obsidian'/directory).glob('*'):
                self.assertEqual(path.read_bytes(),(ROOT/'adhd/obsidian_assets'/directory/path.name).read_bytes())

    def test_cli_cannot_forge_procedure_adoption_and_scan_requires_archive(self):
        import argparse
        from adhd import extra_cli
        parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command');extra_cli.add_parsers(sub)
        with self.assertRaisesRegex(ValueError,'controller-only'):
            extra_cli.execute(parser.parse_args(['wiki','procedure','--operation','adopt']))
        with self.assertRaisesRegex(ValueError,'Archive path'):
            extra_cli.execute(parser.parse_args(['wiki','scan']))


if __name__=='__main__':unittest.main()
