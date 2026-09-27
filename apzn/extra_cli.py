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
    q=sub.add_parser('wiki');q.add_argument('action',choices=['scan','import']);q.add_argument('archive',type=Path)
    q.add_argument('--workspace',type=Path,default=Path.cwd());q.add_argument('--include-records',action='store_true')
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
            if args.action=='recall':return m.recall(args.query,scope,environment=fingerprint(environment_signature(args.workspace)))
            if args.action=='forget':return m.forget(args.id)
            if args.action=='resolve':return m.resolve(args.id,evidence=args.evidence)
            p=payload(args)
            if not isinstance(p,dict) or 'scope' in p or 'verified_receipt' in p or p.get('kind')=='procedure' or p.get('basis')=='verified_run':raise ValueError('CLI memory writes cannot forge controller-verified procedures or override scope')
            return m.put(scope=scope,**p)
    if args.command=='wiki':
        from .wiki import scan_zip,import_zip
        return scan_zip(args.archive) if args.action=='scan' else import_zip(args.archive,args.workspace,include_records=args.include_records)
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
        return run_evaluation(args.out)
    raise ValueError('Unknown capability')
