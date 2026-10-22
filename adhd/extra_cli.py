"""Small explicit capabilities, normally invoked by the Codex agent, not the user."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .core import atomic_json


def add_parsers(sub):
    q=sub.add_parser('coordinate',help='Automatically serialize conflicting project tasks')
    q.add_argument('action',choices=['register','acquire','finish','message','status','run'])
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--payload-file',type=Path)
    q=sub.add_parser('workspace-storage',help='Owned scratch, reservations and bounded archive extraction')
    q.add_argument('action',choices=['sweep','extract','release-cache','run'])
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--payload-file',type=Path)
    q.add_argument('--max-bytes',type=int,help='Scratch budget for direct --argv runs')
    q.add_argument('--timeout',type=float,help='Producer timeout for direct --argv runs')
    q.add_argument('--owner',help='Owner label for direct --argv runs')
    q.add_argument('--argv',nargs=argparse.REMAINDER,help='Run this explicit argv in owned scratch; put last')
    q=sub.add_parser('objects',help='Explicitly capture completed evidence and manage scoped immutable references')
    q.add_argument('action',choices=['capture','read','export','restore','forget','audit','gc','apply','recover'])
    q.add_argument('--root',type=Path);q.add_argument('--workspace',type=Path,default=Path.cwd())
    q.add_argument('--payload-file',type=Path)
    q.add_argument('--operation-id',help='Persisted dry-run operation required for apply')
    q=sub.add_parser('snapshot',help='Immutable reader inputs and generation-checked writer transition')
    q.add_argument('action',choices=['prepare','read','claim-writer','capabilities'])
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--payload-file',type=Path)
    q=sub.add_parser('context',help='Bounded, source-scoped context and continuation sidecars')
    q.add_argument('action',choices=['locate','read','handoff','trace','status'])
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--query',default='')
    q.add_argument('--payload-file',type=Path);q.add_argument('--revision');q.add_argument('--session')
    q.add_argument('--actor',default='parent');q.add_argument('--epoch',default='0')
    q.add_argument('--environment');q.add_argument('--no-global',action='store_true')
    q.add_argument('--sources',default='code,memory,wiki');q.add_argument('--complete',action='store_true')
    q.add_argument('--limit',type=int,default=6);q.add_argument('--max-chars',type=int,default=6500)
    q.add_argument('--start-line',type=int);q.add_argument('--end-line',type=int)
    q.add_argument('--start-char',type=int,help='Decoded character offset within the starting line')
    q.add_argument('--after');q.add_argument('--write',action='store_true');q.add_argument('--markdown',action='store_true')
    q=sub.add_parser('experiment',help='All-start metadata registry and preregistered rollout gates')
    q.add_argument('action',choices=['start','finish','usable','summary','preregister','compare'])
    q.add_argument('--payload-file',type=Path);q.add_argument('--root',type=Path)
    q=sub.add_parser('diagnose',help='Advisory capability, recovery, check-input and review analysis')
    q.add_argument('action',choices=['capabilities','recovery','manifest','calibration'])
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--payload-file',type=Path,required=True)
    q=sub.add_parser('memory');q.add_argument('action',choices=['put','recall','forget','resolve','stats'])
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--payload-file',type=Path)
    q.add_argument('--query',default='');q.add_argument('--id');q.add_argument('--evidence',default='')
    q.add_argument('--global-scope',action='store_true',help='Only explicit cross-project preferences, never inferred personal details')
    q.add_argument('--limit',type=int,default=6);q.add_argument('--max-chars',type=int,default=6500)
    q.add_argument('--project')
    q=sub.add_parser('wiki');q.add_argument('action',choices=['scan','import','configure','context','project','read','forget',
        'record','record-completion','feedback','review','apply','usage','usage-record','procedure','storage','template','open-link'])
    q.add_argument('archive',nargs='?',type=Path)
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--include-records',action='store_true')
    q.add_argument('--payload-file',type=Path);q.add_argument('--query',default='');q.add_argument('--id')
    q.add_argument('--section');q.add_argument('--revision');q.add_argument('--project');q.add_argument('--as-of')
    q.add_argument('--limit',type=int,default=6);q.add_argument('--max-chars',type=int,default=6500)
    q.add_argument('--expand-links',action='store_true')
    q.add_argument('--operation',choices=['list','propose','adopt','rollback','inventory','audit','archive','restore','preview','apply','restore-templates'])
    q=sub.add_parser('route');q.add_argument('query',nargs='?',default='');q.add_argument('--configure-wiki-router',type=Path)
    q.add_argument('--workspace',type=Path,default=Path.cwd())
    q=sub.add_parser('doc');q.add_argument('action',choices=['inspect','edit','edit-hwp','render','capabilities'])
    q.add_argument('source',nargs='?',type=Path);q.add_argument('--out',type=Path);q.add_argument('--payload-file',type=Path)
    q.add_argument('--backend',choices=['auto','hancom'],default='auto')
    q=sub.add_parser('extension');q.add_argument('action',choices=['stage','apply','probe','launch','rollback','status'])
    q.add_argument('--id');q.add_argument('--payload-file',type=Path);q.add_argument('--reviewed',action='store_true')
    q.add_argument('--agents-home',type=Path);q.add_argument('--codex-home',type=Path)
    q.add_argument('--probe-launch',action='store_true',help=argparse.SUPPRESS)
    q=sub.add_parser('provenance');q.add_argument('action',choices=['verify'])
    q.add_argument('manifest',type=Path);q.add_argument('--workspace',type=Path,default=Path.cwd())
    q=sub.add_parser('eval');q.add_argument('--out',type=Path,required=True)
    q.add_argument('--profile',choices=['simple','standard','deep'],default='standard')


def payload(args):
    if not args.payload_file or args.payload_file.stat().st_size>200000:raise ValueError('A bounded JSON payload file is required')
    result=json.loads(args.payload_file.read_text(encoding='utf-8-sig'))
    if not isinstance(result,dict):raise ValueError('Payload must be a JSON object')
    return result


def execute(args):
    if args.command=='coordinate':
        from .coordination import Coordinator
        co=Coordinator(args.workspace)
        if args.action=='status':return co.status()
        p=payload(args)
        if args.action=='register':return co.register(p)
        if args.action=='acquire':return co.acquire(p['task_id'],owner=p.get('owner'))
        if args.action=='finish':return co.finish(p['task_id'],p['generation'],owner=p['owner'],success=p.get('success',True))
        if args.action=='message':return co.negotiate(p['conflict_id'],p['message'])
        return co.run(p['card'],p['argv'],wait_seconds=p.get('wait_seconds',10),timeout=p.get('timeout',900))
    if args.command=='workspace-storage':
        from .workspace_storage import WorkspaceStorage
        direct = getattr(args,'argv',None)
        options = {name:getattr(args,flag,None) for name,flag in
                   (('reserved_bytes','max_bytes'),('timeout','timeout'),('owner','owner'))}
        if direct is not None:
            if args.action!='run' or args.payload_file:
                raise ValueError('--argv is only for run and cannot be combined with --payload-file')
            p={'argv':direct,'cwd':'scratch',**{k:v for k,v in options.items() if v is not None}}
        elif any(v is not None for v in options.values()):
            raise ValueError('--max-bytes, --timeout and --owner require --argv')
        else:
            p=None
        manager=WorkspaceStorage(args.workspace)
        if args.action=='sweep':return manager.sweep()
        p=p if p is not None else payload(args)
        if args.action=='extract':
            path=manager.extract_zip_selected(Path(p['archive']),p['members'],namespace=p['namespace'],
                tool_version=p['tool_version'],options=p.get('options',{}),max_bytes=p.get('max_bytes',512*1024**2))
            return {'cache_path':str(path.relative_to(manager.workspace))}
        if args.action=='release-cache':
            return manager.release_cache(manager.workspace/Path(p['cache_path']))
        from .workspace_commands import run_in_scratch
        return run_in_scratch(manager,p)
    if args.command=='objects':
        from .immutable_store import ImmutableStore
        if args.action=='apply' and not args.operation_id:
            raise ValueError('objects apply requires --operation-id from a current dry-run')
        if args.action!='apply' and args.operation_id:
            raise ValueError('--operation-id applies only to objects apply')
        if args.action=='capture':
            from .completed_archive import capture_completed
            p=payload(args)
            if set(p)-{'owner_key','policy_revision','privacy','source','retention'}:
                raise ValueError('Unexpected completed-capture field')
            if 'source' in p:
                selected=Path(p['source'])
                p['source']=selected if selected.is_absolute() else args.workspace/selected
            return capture_completed(args.workspace,root=args.root,**p)
        if args.action in {'read','export','restore','forget'}:
            p=payload(args)
            allowed={'policy_revision','allowed_privacy'}|{
                'read':{'ref_id','offset','limit'},'export':{'ref_id','destination'},
                'restore':{'export_path','destination'},'forget':{'ref_id'}}[args.action]
            if set(p)-allowed:
                raise ValueError('Unexpected object access field')
            privacy=p.pop('allowed_privacy',None)
            revision=p.pop('policy_revision',None)
            if (not isinstance(revision,str) or not revision or not isinstance(privacy,list)
                    or not privacy or len(privacy)>8 or any(not isinstance(item,str) for item in privacy)):
                raise ValueError('Expected policy_revision and restrictive allowed_privacy are required')
            # The common store boundary reloads workspace-owned immutable-policy.json.
            # These request values can narrow that policy, never grant access.
            access={'workspace':args.workspace,'policy_revision':revision,'allowed_privacy':privacy}
            if args.action=='restore':
                return ImmutableStore.restore(Path(p['export_path']),Path(p['destination']),access)
            with ImmutableStore(args.root) as objects:
                if args.action=='export':return objects.export(p['ref_id'],access,Path(p['destination']))
                if args.action=='forget':return objects.forget(p['ref_id'],access)
                import base64
                offset=p.get('offset',0);limit=p.get('limit',65536)
                body=objects.read(p['ref_id'],access,offset=offset,limit=limit)
                return {'ref_id':p['ref_id'],'encoding':'base64','data':base64.b64encode(body).decode('ascii'),
                        'offset':offset,'bytes':len(body),'may_have_more':len(body)==limit,
                        'next_offset':offset+len(body)}
        with ImmutableStore(args.root) as objects:
            if args.action=='audit':return objects.audit()
            if args.action=='apply':return objects.gc_apply(args.operation_id)
            if args.action=='recover':return objects.gc_recover()
            return objects.gc(dry_run=True)
    if args.command=='snapshot':
        from . import snapshot_reader as reader
        if args.action=='capabilities':return reader.concurrent_support()
        p=payload(args)
        if args.action=='prepare':
            if 'scratch' in p:p['scratch']=Path(p['scratch'])
            return reader.prepare(args.workspace,**p)
        p['snapshot']=Path(p['snapshot'])
        if args.action=='read':return reader.read(**p)
        return reader.claim_writer(**p)
    if args.command=='context':
        from .context_session import ContextSession,handoff,status
        environment=args.environment or ''
        if args.action=='locate' and 'memory' in args.sources.split(',') and args.environment is None:
            from .core import environment_signature
            from .memory import fingerprint
            environment=args.environment if args.environment is not None else fingerprint(environment_signature(args.workspace))
        if args.action=='locate':
            from .context_search import locate
            return locate(args.workspace,args.query,limit=args.limit,max_chars=args.max_chars,
                          complete=args.complete,sources=tuple(args.sources.split(',')),
                          environment=environment,include_global=not args.no_global)
        if args.action=='read':
            from .context_search import read
            p=payload(args);ref=p.get('ref',p)
            if ref.get('source')=='memory' and args.environment is None:
                from .core import environment_signature
                from .memory import fingerprint
                environment=fingerprint(environment_signature(args.workspace))
            revision=args.revision or p.get('expected_revision')
            if not revision:raise ValueError('context read requires an expected revision')
            result=read(args.workspace,ref,expected_revision=revision,
                        start_line=args.start_line if args.start_line is not None else p.get('start_line'),
                        end_line=args.end_line if args.end_line is not None else p.get('end_line'),
                        start_char=args.start_char if args.start_char is not None else p.get('start_char',0),
                        max_chars=args.max_chars,environment=environment,include_global=not args.no_global)
            if args.session and result.get('status')=='ok':
                with ContextSession(args.workspace,args.session) as ledger:
                    delivery=ledger.deliver(result.get('ref',ref),result.get('text',''),actor=args.actor,
                                            epoch=args.epoch,max_chars=args.max_chars)
                result.update(text=delivery['excerpt'],delivery=delivery)
            return result
        if not args.session:raise ValueError('A native/context session is required')
        if args.action=='handoff':return handoff(args.workspace,args.session,max_chars=args.max_chars)
        if args.action=='status':
            result=status(args.workspace,args.session,write=args.write)
            return {'markdown':result['markdown']} if args.markdown else result
        with ContextSession(args.workspace,args.session) as ledger:
            return ledger.trace(limit=args.limit,after=args.after)
    if args.command=='experiment':
        from .trial_registry import TrialRegistry,rollout_gate
        registry=TrialRegistry(args.root)
        if args.action=='summary':return registry.summary()
        p=payload(args)
        if args.action=='start':return {'trial_id':registry.start(**p)}
        if args.action=='finish':registry.finish(**p);return {'status':'recorded'}
        if args.action=='usable':return {'first_usable_ms':registry.usable(p['trial_id'],Path(p['receipt']))}
        if args.action=='preregister':return registry.preregister(p)
        return rollout_gate(p,list(registry.trials()))
    if args.command=='diagnose':
        p=payload(args)
        if args.action=='capabilities':
            from .host_capabilities import capability_inventory
            return capability_inventory(p.get('configured',[]),p.get('exposed'),p.get('observations',[]))
        if args.action=='recovery':
            from .recovery import diagnosis
            return diagnosis(args.workspace,**p)
        if args.action=='manifest':
            from .validation_batch import suggest_inputs
            return suggest_inputs(args.workspace,p)
        from .review_calibration import calibrate
        return calibrate(args.workspace,p['cases'],p['reviews'])
    if args.command=='memory':
        from .memory import Memory,namespace,fingerprint
        from .core import environment_signature
        scope='global' if args.global_scope else namespace(args.workspace)
        with Memory() as m:
            if args.action=='stats':return m.stats()
            if args.action=='recall':return m.recall_with_wiki(args.query,scope,workspace=args.workspace,
                limit=args.limit,max_chars=args.max_chars,project=args.project,
                environment=fingerprint(environment_signature(args.workspace)))
            if args.action=='forget':return m.forget(args.id)
            if args.action=='resolve':return m.resolve(args.id,evidence=args.evidence)
            p=payload(args)
            if not isinstance(p,dict) or 'scope' in p or 'verified_receipt' in p or p.get('kind')=='procedure' or p.get('basis')=='verified_run':raise ValueError('CLI memory writes cannot forge controller-verified procedures or override scope')
            return m.put(scope=scope,**p)
    if args.command=='wiki':
        if args.action in {'scan','import'}:
            from .wiki import scan_zip,import_zip
            if not args.archive:raise ValueError('Archive path is required for scan/import')
            return scan_zip(args.archive) if args.action=='scan' else import_zip(args.archive,args.workspace,include_records=args.include_records)
        from . import obsidian,experience,experience_procedures,experience_storage
        if args.action=='configure':return obsidian.configure(args.workspace,payload(args))
        if args.action=='context':return obsidian.context(args.workspace,args.query,limit=args.limit,max_chars=args.max_chars,
            project=args.project,as_of=args.as_of,expand_links=args.expand_links)
        if args.action=='project':return obsidian.project(args.workspace,args.project or args.query,
            limit=args.limit,max_chars=args.max_chars,as_of=args.as_of,expand_links=args.expand_links)
        if args.action=='read':
            from .obsidian_assets.bridge import read_note
            return read_note(args.workspace,args.id,section=args.section,revision=args.revision,max_chars=args.max_chars)
        if args.action=='forget':
            from .obsidian_assets.bridge import forget_note
            return forget_note(args.workspace,args.id)
        if args.action in {'record','feedback'}:
            event=payload(args)
            if args.action=='feedback':event={**event,'kind':'feedback'}
            return experience.candidate(args.workspace,event)
        if args.action=='record-completion':
            from .core import capture_wiki_completion
            return capture_wiki_completion(args.workspace,payload(args)['completion_receipt'])
        if args.action=='review':return experience.list_candidates(args.workspace)
        if args.action=='apply':return experience.apply_candidate(args.workspace,args.id)
        if args.action=='usage':return experience.usage_report(args.workspace)
        if args.action=='usage-record':return experience.record_usage(args.workspace,payload(args))
        if args.action=='procedure':
            if args.operation=='adopt':raise ValueError('Procedure adoption is controller-only after independent completion')
            if args.operation=='propose':return experience_procedures.propose(args.workspace,payload(args))
            if args.operation=='rollback':
                data=payload(args);return experience_procedures.rollback(args.workspace,data['procedure_id'],data.get('version'))
            if args.operation in {None,'list'}:return experience_procedures.list_procedures(args.workspace)
            raise ValueError('Unknown procedure operation')
        if args.action=='storage':
            if args.operation in {None,'inventory'}:
                from .obsidian_assets.bridge import storage_inventory
                return storage_inventory(args.workspace)
            if args.operation=='audit':return experience_storage.audit_references(args.workspace,payload(args).get('references'))
            if args.operation=='archive':return experience_storage.archive(args.workspace,payload(args))
            if args.operation=='restore':return experience_storage.restore(args.workspace,payload(args))
            raise ValueError('Unknown storage operation')
        if args.action in {'template','open-link'}:
            from .obsidian_assets.bridge import provision,restore_templates,open_link
            if args.action=='open-link':return open_link(args.workspace,args.id,args.section)
            if args.operation=='restore-templates':return restore_templates(args.workspace,payload(args))
            return provision(args.workspace,payload(args) if args.payload_file else {},apply=args.operation=='apply')
    if args.command=='route':
        from .skill_router import configure_router,route_skills
        return configure_router(args.configure_wiki_router) if args.configure_wiki_router else route_skills(args.query,workspace=args.workspace)
    if args.command=='doc':
        from .documents import capabilities,inspect_document,edit_text,edit_binary_hwp,render_document
        if args.action=='capabilities':return capabilities()
        if not args.source:raise ValueError('Document source is required')
        if args.action=='inspect':return inspect_document(args.source)
        if not args.out:raise ValueError('A new --out path is required')
        if args.action=='edit':return edit_text(args.source,args.out,payload(args))
        if args.action=='edit-hwp':return edit_binary_hwp(args.source,args.out,payload(args))
        return render_document(args.source,args.out,args.backend)
    if args.command=='extension':
        from . import extensions as e
        if args.action=='stage':return e.stage(payload(args))
        if args.action=='status':return e.status(args.id)
        if not args.id:raise ValueError('--id is required')
        if args.action=='apply':return e.apply(args.id,reviewed=args.reviewed,agents_home=args.agents_home,codex_home=args.codex_home)
        if args.action=='probe':return e.probe(args.id)
        if args.action=='launch':return e.launch(args.id,probe_mode=args.probe_launch)
        return e.rollback(args.id)
    if args.command=='provenance':
        from .provenance import validate_provenance
        return validate_provenance(args.workspace,args.manifest)
    if args.command=='eval':
        from .evaluation import run_evaluation
        return run_evaluation(args.out,execution_profile=args.profile)
    raise ValueError('Unknown capability')
