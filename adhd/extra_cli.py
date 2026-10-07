"""Small explicit capabilities, normally invoked by the Codex agent, not the user."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .core import atomic_json


def add_parsers(sub):
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
    return json.loads(args.payload_file.read_text(encoding='utf-8-sig'))


def execute(args):
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
