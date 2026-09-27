"""Requirements-bound planning and document gates (structural, not semantic proof)."""
from __future__ import annotations
from fnmatch import fnmatchcase
from pathlib import Path
import json
from .core import digest, file_hash


def validate_plan(plan:dict, criteria:list[dict]) -> dict:
    if not isinstance(plan,dict):raise ValueError('A nontrivial run requires an explicit plan object')
    for field in ['objective','approach','verification']:
        if not isinstance(plan.get(field),str) or not plan[field].strip() or len(plan[field])>6000:raise ValueError('Plan requires bounded '+field)
    for field in ['preflight','risks','alternatives']:
        values=plan.get(field)
        if not isinstance(values,list) or not 1<=len(values)<=12 or any(not isinstance(v,str) or not v.strip() or len(v)>2000 for v in values):raise ValueError('Plan needs 1..12 '+field+' entries ("none identified" only if justified)')
    steps=plan.get('steps')
    if not isinstance(steps,list) or not 1<=len(steps)<=20:raise ValueError('Plan requires 1..20 ordered steps')
    ids=set();covered=set();expected={r['id'] for r in criteria}
    for step in steps:
        if not isinstance(step,dict) or not isinstance(step.get('id'),str) or step['id'] in ids:raise ValueError('Plan step ids must be unique strings')
        if not isinstance(step.get('action'),str) or not step['action'].strip():raise ValueError('Plan step needs action')
        deps=step.get('depends_on',[]);reqs=step.get('requirements',[])
        if not isinstance(deps,list) or not set(deps)<=ids:raise ValueError('Plan dependencies must refer to earlier steps; no cycles or unresolved prerequisites')
        if not isinstance(reqs,list) or not reqs or not set(reqs)<=expected:raise ValueError('Every plan step needs known requirement IDs')
        ids.add(step['id']);covered.update(reqs)
    if covered!=expected:raise ValueError('Plan does not cover every requirement')
    return plan


def validate_learning_check(mode:str, prompts:list[dict], value:dict|None,
                            workspace:Path, checked_path) -> dict|None:
    """A study deliverable provides a usable self-check unless full work was requested."""
    if mode!='study':
        if value is not None:raise ValueError('Learning check belongs to study mode')
        return None
    request=' '.join(p.get('text','').lower() for p in prompts)
    full_solution=any(phrase in request for phrase in
        ('완전한 풀이','전체 풀이','정답까지','full solution','complete solution'))
    if value is None:
        if full_solution:return {'format':'full_solution_requested'}
        raise ValueError('Study mode needs a short self-check question and answer key')
    if (not isinstance(value,dict) or set(value)!={'question','answer_key','explanation_file'} or
            any(not isinstance(value.get(k),str) or not value[k].strip() or len(value[k])>2000
                for k in ('question','answer_key','explanation_file')) or
            value['question'].strip()==value['answer_key'].strip()):
        raise ValueError('Learning check needs a distinct question, answer key and explanation file')
    checked_path(workspace,value['explanation_file'],existing=True)
    return value


def validate_document_contract(rows,artifacts,workspace,checked_path):
    if not isinstance(rows,list) or len(rows)>30:raise ValueError('documents must contain at most 30 entries')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('path') not in artifacts or row['path'] in seen:raise ValueError('Document contract path missing/duplicate/not an artifact')
        if set(row)-{'path','max_pages','exact_pages','max_chars','min_chars','count_whitespace','editable_required','editable_elements','layout_checks'}:raise ValueError('Unknown document contract field')
        for k in ['max_pages','exact_pages','max_chars','min_chars']:
            if k in row and (type(row[k]) is not int or row[k]<1):raise ValueError('Positive integer document constraint required')
        for k in ['count_whitespace','editable_required']:
            if k in row and type(row[k]) is not bool:raise ValueError('Boolean document option required')
        if 'editable_elements' in row:
            elements=row['editable_elements']
            if not isinstance(elements,list) or not 1<=len(elements)<=50:raise ValueError('editable_elements needs 1..50 selectors')
            for element in elements:
                if (not isinstance(element,dict) or set(element)-{'selector','type','required'} or
                        not isinstance(element.get('selector'),str) or not element['selector'] or len(element['selector'])>500 or
                        element.get('type') not in {'paragraph','text','shape','table','chart','image','equation','ole'} or
                        type(element.get('required',True)) is not bool):
                    raise ValueError('Invalid editable element selector')
        if 'layout_checks' in row:
            checks=row['layout_checks']
            if not isinstance(checks,list) or not 1<=len(checks)<=20 or any(not isinstance(v,str) or not v.strip() or len(v)>500 for v in checks) or len(checks)!=len(set(checks)):
                raise ValueError('layout_checks needs 1..20 unique bounded descriptions')
        if row.get('min_chars',0)>row.get('max_chars',10**10):raise ValueError('Contradictory character limits')
        if row.get('exact_pages',0)>row.get('max_pages',10**10):raise ValueError('Contradictory page limits')
        checked_path(workspace,row['path']);seen.add(row['path'])
    return rows


def document_candidate(state,payload,checked_path):
    from .documents import inspect_document,validate_render
    ws=Path(state['workspace']).resolve();contracts={r['path']:r for r in state['contract'].get('documents',[])}
    required={p for p in state['contract']['artifacts'] if Path(p).suffix.lower() in {'.docx','.pptx','.hwp','.hwpx','.pdf'}}|set(contracts)
    if not required:return [],[]
    rows=payload.get('document_evidence',[])
    if not isinstance(rows,list):raise ValueError('document_evidence must be an array')
    indexed={}
    for row in rows:
        if not isinstance(row,dict) or row.get('path') in indexed:raise ValueError('Duplicate document evidence')
        indexed[row.get('path')]=row
    if set(indexed)!=required:raise ValueError('Every final office/PDF document needs exact hash-bound render evidence')
    evidence=[];allfiles=[]
    for rel in sorted(required):
        source=checked_path(ws,rel,existing=True);r=indexed[rel];c=contracts.get(rel,{})
        manifest_path=checked_path(ws,r.get('render_manifest',''),existing=True)
        if manifest_path.stat().st_size>1000000:raise ValueError('Render manifest too large')
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        referenced=[manifest.get('pdf','')]+[p.get('image','') for p in manifest.get('pages',[])]
        for value in referenced:
            if not isinstance(value,str) or not Path(value).is_absolute():raise ValueError('Document render evidence paths must be absolute')
            try:relative=Path(value).relative_to(ws)
            except ValueError:raise ValueError('Document render evidence must stay in this workspace')
            checked_path(ws,str(relative),existing=True);allfiles.append(relative.as_posix())
        render_validation=validate_render(manifest,source,max_pages=c.get('max_pages'))
        if not render_validation['derivation_verified']:raise ValueError('Legacy render manifest must be regenerated to prove page-image derivation')
        if c.get('exact_pages') is not None and render_validation['actual_page_count']!=c['exact_pages']:raise ValueError('Exact document page count mismatch')
        inspection=inspect_document(source)
        if c.get('max_chars') is not None or c.get('min_chars') is not None:
            n=inspection.get('chars_with_whitespace' if c.get('count_whitespace',True) else 'chars_without_whitespace')
            if n is None:raise ValueError('Character extraction unavailable; cannot certify length')
            if n>c.get('max_chars',10**10) or n<c.get('min_chars',0):raise ValueError('Document character limit mismatch')
        objects=inspection.get('objects',[])
        if c.get('editable_required') and not any(o.get('editable') for o in objects):
            raise ValueError('Document has no verified native editable elements')
        matched_elements=[]
        for requirement in c.get('editable_elements',[]):
            matches=[o for o in objects if o.get('type')==requirement['type'] and fnmatchcase(o.get('element_id',''),requirement['selector'])]
            editable=[o for o in matches if o.get('editable') is True]
            if requirement.get('required',True) and not editable:
                raise ValueError('Required editable element is missing or not natively editable: '+requirement['type']+' '+requirement['selector'])
            matched_elements.append({'selector':requirement['selector'],'type':requirement['type'],'required':requirement.get('required',True),
                                     'matched':[o['element_id'] for o in editable]})
        allfiles.append(manifest_path.relative_to(ws).as_posix())
        evidence.append({'path':rel,'render_manifest':manifest_path.relative_to(ws).as_posix(),
          'render_sha256':file_hash(manifest_path),'pages':render_validation['pages'],
          'renderer':manifest['renderer'],'render_derivation_verified':True,'editable_elements':matched_elements,
          'layout_checks':c.get('layout_checks',[]),'visual_approval':'pending_independent_review'})
    return evidence,list(dict.fromkeys(allfiles))


def review_documents(state,verdict):
    expected={r['path']:r for r in state['candidate'].get('document_evidence',[])}
    if not expected:return
    rows=verdict.get('document_reviews')
    if not isinstance(rows,list) or len(rows)!=len(expected):raise ValueError('Verifier must report a visual review for every final document')
    seen=set()
    for r in rows:
        if not isinstance(r,dict) or r.get('path') not in expected or r['path'] in seen:raise ValueError('Invalid document review')
        e=expected[r['path']]
        if r.get('render_sha256')!=e['render_sha256'] or r.get('inspected_pages')!=e['pages'] or r.get('pass') is not True or not r.get('evidence'):
            raise ValueError('Missing all-page visual review or mismatched render hash')
        if e.get('layout_checks') and r.get('inspected_layout_checks')!=e['layout_checks']:
            raise ValueError('Verifier did not report every required layout check')
        seen.add(r['path'])
