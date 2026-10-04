from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import subprocess

from . import DISPLAY_NAME
from .core import MODES, atomic_json, home, read_json, recipes, select_mode, skill_search, store
from .install import uninstall
from .native_install import (audit_native as audit, install_native, rollback_native,
                             upgrade_native, _find_managed_installation)
from .native import submit_request, bridge, bounded_json
from .runner import execute_run, make_run, run_folder


def output(value):
    payload=json.dumps(value, ensure_ascii=False, indent=2)
    if hasattr(sys.stdout,'buffer'):
        sys.stdout.buffer.write((payload+'\n').encode('utf-8'))
    else:
        print(payload)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f'{DISPLAY_NAME} - native Codex App/CLI orchestration, with optional legacy batch loops.')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ['install', 'upgrade', 'uninstall', 'doctor', 'rollback-native']:
        q = sub.add_parser(name)
        q.add_argument('--codex-home', type=Path, default=home())
        if name in {'install','upgrade'}:
            q.add_argument('--agents-home', type=Path)
            q.add_argument('--migrate-existing-roles', action='store_true',
                           help='Opt in to rewriting existing agent model pins; default preserves them.')
            q.add_argument('--compact', action='store_true', help='Replace eager global guidance by a compact router; original preserved. Opt-in, not default.')
    q = sub.add_parser('builtin', help='Manage the default skill bundle and MCP registrations only')
    q.add_argument('action', choices=['apply', 'status', 'probe', 'enable', 'rollback'])
    q.add_argument('key', nargs='?', help='MCP key for enable')
    q.add_argument('--codex-home', type=Path, default=home())
    q.add_argument('--agents-home', type=Path)
    q.add_argument('--probe-file', type=Path, help='Reviewed read-only extension probe-call JSON')
    q.add_argument('--consent', action='store_true', help='Explicitly authorize this connection and the specified read probe')
    q.add_argument('--oauth-token-env', help='Name of an environment variable holding an already authorized OAuth access token')
    q.add_argument('--probe-timeout', type=int, default=25)
    q = sub.add_parser('run')
    q.add_argument('goal', nargs='?')
    q.add_argument('--goal-file', type=Path)
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q.add_argument('--mode', choices=('auto',) + MODES, default='auto')
    q.add_argument('--codex', default='codex')
    q.add_argument('--model', default='gpt-6-sol', help='Explicitly selected, locally available model id; otherwise inherit existing configuration.')
    q.add_argument('--effort', choices=('inherit', 'low', 'medium', 'high', 'xhigh','ultra','max'), default='max')
    q.add_argument('--iterations', type=int, default=8)
    q.add_argument('--max-seconds', type=int, default=3600)
    q.add_argument('--max-tokens', type=int, default=120000)
    q.add_argument('--call-timeout', type=int, default=900)
    q.add_argument('--check-timeout', type=int, default=300)
    q.add_argument('--check', action='append', default=[], metavar='JSON_ARGV', help='Explicitly authorize a host-side check, e.g. ["python","-m","pytest","-q"]. These run OUTSIDE Codex sandbox.')
    q.add_argument('--allow-mcp', action='append', default=[], help='Keep this registered MCP in the loop. External side effects need separate authorization.')
    q.add_argument('--allow-plugin', action='append', default=[], help='Keep an existing fully-qualified plugin, e.g. documents@openai-primary-runtime.')
    q.add_argument('--catalog-budget', type=int, default=0, help='Opt-in skills.max_context_tokens override; first verify installed Codex supports it. Zero leaves default unchanged.')
    q.add_argument('--allow-unknown-usage', action='store_true', help='Allow versions not emitting usage; token ceiling then cannot be enforced.')
    q.add_argument('--prepare-only', action='store_true', help='Save a run request without making a model call.')
    q = sub.add_parser('resume')
    q.add_argument('run_id')
    q.add_argument('--extra-iterations', type=int, default=0)
    q.add_argument('--extra-seconds', type=int, default=0)
    q.add_argument('--extra-tokens', type=int, default=0)
    q.add_argument('--allow-unknown-usage', action='store_true')
    for name in ['status', 'stop']:
        q = sub.add_parser(name)
        q.add_argument('run_id')
    q = sub.add_parser('skills')
    q.add_argument('query')
    q.add_argument('--limit', type=int, default=5)
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q = sub.add_parser('recall')
    q.add_argument('query')
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q.add_argument('--mode', choices=MODES, default='coding')
    q = sub.add_parser('native', help='Sandbox-side requests to the native App/CLI controller')
    q.add_argument('operation', choices=['begin','plan','checkpoint','candidate','sync-intent','attach-large','pause','blocked','status'])
    q.add_argument('--session', required=True)
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q.add_argument('--payload-file', type=Path)
    q.add_argument('--profile', choices=['simple', 'standard', 'deep'], help='Explicit execution intensity for begin; preserves selected models and effort')
    q = sub.add_parser('check', help='Run an explicit argv check and emit a structured execution receipt')
    q.add_argument('--spec-file', required=True, type=Path)
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q = sub.add_parser('coding-scope', help='Capture or verify a read-only Git change-scope audit')
    q.add_argument('operation', choices=['capture', 'audit', 'verify'])
    q.add_argument('--workspace', type=Path, default=Path.cwd())
    q.add_argument('--repository', default='.')
    q.add_argument('--allow', action='append', default=[])
    q.add_argument('--baseline')
    q.add_argument('--report')
    q.add_argument('--out')
    from .extra_cli import add_parsers
    add_parsers(sub)
    from .large_cli import add_parsers as add_large_parsers
    add_large_parsers(sub)
    return p


def main(argv=None) -> int:
    if os.name == 'nt':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    try:
        if args.command == 'builtin':
            from .builtin import apply_builtin, builtin_status, probe_builtin, enable_builtin, rollback_builtin
            if args.action == 'apply':
                output(apply_builtin(args.codex_home, args.agents_home))
            elif args.action == 'status':
                output(builtin_status(args.codex_home, args.agents_home))
            elif args.action == 'probe':
                if not args.key:
                    raise ValueError('builtin probe requires an MCP key')
                call = bounded_json(args.probe_file) if args.probe_file else None
                output(probe_builtin(args.codex_home, args.key, probe_call=call, consent=args.consent,
                                     oauth_token_env=args.oauth_token_env, timeout=args.probe_timeout))
            elif args.action == 'enable':
                if not args.key:
                    raise ValueError('builtin enable requires an MCP key')
                output(enable_builtin(args.codex_home, args.key))
            else:
                output(rollback_builtin(args.codex_home))
            return 0
        if args.command == 'extension' and args.action == 'launch':
            if args.codex_home:
                os.environ['CODEX_HOME']=str(args.codex_home.expanduser().resolve())
            from .extra_cli import execute
            return int(execute(args))
        if args.command in {'large', 'batch'}:
            from .large_cli import execute
            result = execute(args)
            output(result)
            return 2 if isinstance(result, dict) and result.get('status') in {'needs_repair', 'stale', 'blocked', 'failed'} else 0
        elif args.command in {'memory','wiki','route','doc','extension','provenance','eval'}:
            from .extra_cli import execute
            output(execute(args))
        elif args.command == 'check':
            from .evidence import run_check
            if not args.spec_file.is_file() or args.spec_file.stat().st_size>200000:
                raise ValueError('A bounded JSON --spec-file is required')
            spec=json.loads(args.spec_file.read_text(encoding='utf-8-sig'))
            output(run_check(spec,args.workspace.resolve()))
        elif args.command == 'coding-scope':
            from .coding_scope import capture, audit as scope_audit, _path
            from .core import digest, file_hash
            ws = args.workspace.resolve()
            if args.operation == 'capture':
                value = capture(ws, args.repository, args.allow)
            else:
                if not args.baseline:
                    raise ValueError('A captured --baseline is required')
                value = scope_audit(ws, bounded_json(_path(ws, args.baseline), 2 * 1024 * 1024))
            if args.operation == 'verify':
                if not args.report or digest(value) != digest(bounded_json(_path(ws, args.report), 2 * 1024 * 1024)):
                    raise ValueError('Scope report no longer matches the actual Git audit')
                output({'verified': True, 'changes': value['changes']})
            else:
                if not args.out:
                    raise ValueError('A new relative --out path is required')
                target = _path(ws, args.out)
                if target.exists():
                    raise ValueError('Refusing to overwrite a scope baseline or report')
                atomic_json(target, value)
                output({'path': args.out, 'sha256': file_hash(target)})
        elif args.command == 'upgrade':
            output(upgrade_native(args.codex_home,args.agents_home,args.compact,args.migrate_existing_roles))
        elif args.command == 'install':
            output(install_native(args.codex_home, args.agents_home, args.compact, args.migrate_existing_roles))
        elif args.command == 'rollback-native':
            output(rollback_native(args.codex_home))
        elif args.command == 'native':
            payload=bounded_json(args.payload_file) if args.payload_file else {}
            if args.profile:
                if args.operation != 'begin':
                    raise ValueError('--profile applies only to native begin')
                if payload.get('execution_profile', args.profile) != args.profile:
                    raise ValueError('CLI and payload execution profiles differ')
                payload['execution_profile'] = args.profile
            output(submit_request(args.session,args.workspace,args.operation,payload))
        elif args.command == 'uninstall':
            native_record = _find_managed_installation(args.codex_home) is not None
            output(rollback_native(args.codex_home) if native_record else uninstall(args.codex_home))
        elif args.command == 'doctor':
            output(audit(args.codex_home))
        elif args.command == 'skills':
            output(skill_search(args.query, args.limit, workspace=args.workspace))
        elif args.command == 'recall':
            output(recipes(args.workspace.resolve(), args.mode, args.query))
        elif args.command == 'run':
            goal = args.goal_file.read_text(encoding='utf-8-sig') if args.goal_file else args.goal
            if not goal:
                raise ValueError('Provide a goal or --goal-file')
            positive = [args.iterations, args.max_seconds, args.max_tokens, args.call_timeout, args.check_timeout]
            if min(positive) <= 0:
                raise ValueError('All execution budgets must be positive')
            if not 0 <= args.catalog_budget <= 10000:
                raise ValueError('Catalog budget must be 0..10000')
            checks = [json.loads(s) for s in args.check]
            for c in checks:
                if not isinstance(c, list) or not c or not all(isinstance(x, str) and x for x in c):
                    raise ValueError('--check requires a nonempty JSON array of argv strings, not shell code')
            settings = {'codex': args.codex, 'model': args.model, 'effort': args.effort,
                        'max_iterations': args.iterations, 'max_seconds': args.max_seconds,
                        'max_tokens': args.max_tokens, 'call_timeout': args.call_timeout,
                        'check_timeout': args.check_timeout, 'checks': checks,
                        'allow_mcp': args.allow_mcp, 'allow_plugin': args.allow_plugin,
                        'catalog_budget': args.catalog_budget, 'allow_unknown_usage': args.allow_unknown_usage}
            run_id = make_run(goal, args.workspace, select_mode(goal) if args.mode == 'auto' else args.mode, settings)
            print(f'Run ID: {run_id}', flush=True)
            if args.prepare_only:
                output({'run_id': run_id, 'status': 'initialized', 'path': str(run_folder(run_id))})
            else:
                state = execute_run(run_id)
                output({'run_id': run_id, 'status': state['status'], 'reason': state['reason'], 'usage': state['usage'],
                        'path': str(run_folder(run_id))})
                return 0 if state['status'] == 'complete' else 2
        elif args.command == 'resume':
            state = execute_run(args.run_id, args.extra_iterations, args.extra_seconds, args.extra_tokens, args.allow_unknown_usage)
            output({'run_id': args.run_id, 'status': state['status'], 'reason': state['reason'], 'usage': state['usage']})
            return 0 if state['status'] == 'complete' else 2
        elif args.command in ('status', 'stop'):
            folder = run_folder(args.run_id)
            state = read_json(folder / 'state.json')
            if not state:
                raise ValueError('Unknown run id')
            if args.command == 'stop':
                (folder / 'STOP').write_text('user stop\n', encoding='utf-8')
                output({'run_id': args.run_id, 'stop_requested': True})
            else:
                output(state)
        return 0
    except (ValueError, OSError, ImportError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
