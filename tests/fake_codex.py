"""Test double only. Does not call any model or reproduce a real Codex sandbox."""
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
if '--help' in args:
    print('--output-schema --json --sandbox --output-last-message')
    sys.exit(0)
scenario = os.environ.get('FAKE_SCENARIO', 'success')
if scenario == 'exit_error':
    print('simulated CLI/auth failure', file=sys.stderr)
    sys.exit(17)
kind = Path(args[args.index('--output-schema')+1]).stem
output = Path(args[args.index('-o')+1])
workspace = Path(args[args.index('-C')+1])
state_path = Path(os.environ['FAKE_COUNTER'])
counters = json.loads(state_path.read_text()) if state_path.exists() else {}
counters[kind] = counters.get(kind, 0) + 1
state_path.write_text(json.dumps(counters))
sys.stdin.read()
criteria = [{'id': 'R1', 'status': 'pass', 'evidence': 'answer.txt inspected; expected content matches'}]
if kind == 'plan':
    result = {'summary':'Produce requested artifact','criteria':[{'id':'R1','requirement':'Write answer.txt'}],
              'deliverables':['answer.txt'], 'non_goals':[], 'assumptions':[], 'verification_plan':['Inspect answer.txt']}
    if scenario == 'bad_path': result['deliverables'] = ['../outside.txt']
    if scenario == 'no_deliverables': result['deliverables'] = []
elif kind == 'work':
    if scenario not in ('missing_artifact','no_progress'):
        (workspace / 'answer.txt').write_text('verified answer revision ' + str(counters[kind]))
    status = 'continuing' if scenario == 'no_progress' or scenario == 'continue_once' and counters[kind] == 1 else 'complete'
    result = {'status':status,'summary':'work checkpoint','criteria':criteria,'next_action':'Verify the artifact',
              'reusable_steps':['Inspect the source','Write and test answer.txt','Validate against the original request']}
    if scenario == 'wrong_id': result['criteria'][0]['id'] = 'WRONG'
    if scenario == 'worker_blocked': result['status']='blocked';result['summary']='missing authorized data'
elif kind == 'review':
    result = {'verdict':'approve','summary':'Actual artifact inspected','criteria':criteria,'risks':[]}
    if scenario == 'revise_once' and counters[kind] == 1:
        result['verdict']='revise';result['criteria'][0]['status']='fail';result['summary']='Add missing edge case'
    if scenario == 'incomplete_review': result['criteria']=[]
    if scenario == 'review_blocked': result['verdict']='blocked';result['summary']='Cannot inspect requested rendering'
output.write_text(json.dumps(result), encoding='utf-8')
if scenario == 'malformed_result': output.write_text('not json')
if scenario != 'unknown_usage':
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':50,'output_tokens':20}}))
