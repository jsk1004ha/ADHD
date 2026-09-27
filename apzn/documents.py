"""Conservative document adapters and hash-bound render evidence.

Editing and rendering are deliberately separate. XML well-formedness is not visual
fidelity; a page image's existence is not proof a reviewer inspected it.
"""
from __future__ import annotations
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from .core import file_hash, atomic_json

OOXML={'.docx':('word/document.xml','word/'),'.pptx':('ppt/presentation.xml','ppt/')}
MAX_TOTAL=256*1024*1024


def _new_output(source: Path,out: Path):
    source=source.resolve(); out=out.absolute()
    if not source.is_file(): raise ValueError('Source file not found')
    if source==out.resolve() or out.exists() or out.is_symlink():
        raise ValueError('Use a NEW output path; original and existing outputs are never overwritten')
    out.parent.mkdir(parents=True,exist_ok=True)
    return source,out


def zip_parts(path: Path) -> dict[str,bytes]:
    with zipfile.ZipFile(path) as z:
        infos=z.infolist()
        if len(infos)>12000 or sum(i.file_size for i in infos)>MAX_TOTAL:
            raise ValueError('Document package exceeds inspection budget')
        names=[i.filename for i in infos]
        if len(names)!=len(set(names)): raise ValueError('Duplicate ZIP member')
        for i in infos:
            name=i.filename.replace('\\','/')
            if name.startswith('/') or '..' in PurePosixPath(name).parts or re.match(r'^[A-Za-z]:',name):
                raise ValueError('Unsafe ZIP member')
            if i.flag_bits&1:raise ValueError('Encrypted document needs authorized native handling')
        return {i.filename:z.read(i) for i in infos if not i.is_dir()}


def _xml(data: bytes):
    from lxml import etree
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DTD/entity declarations are not accepted')
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False,no_network=True,remove_blank_text=False))


def detect(path: Path) -> str:
    with path.open('rb') as f: head=f.read(16)
    if head.startswith(b'%PDF-'): return 'pdf'
    if head.startswith(bytes.fromhex('D0CF11E0A1B11AE1')): return 'cfb_binary_requires_native_or_hwpkit'
    if zipfile.is_zipfile(path):
        parts=zip_parts(path)
        if 'word/document.xml' in parts:return 'docx'
        if 'ppt/presentation.xml' in parts:return 'pptx'
        if parts.get('mimetype',b'').strip()==b'application/hwp+zip' or any(re.fullmatch(r'Contents/section\d+\.xml',p) for p in parts):return 'hwpx'
        return 'unknown_zip'
    return 'unknown'


def inspect_document(path: Path) -> dict:
    path=path.resolve(); kind=detect(path)
    result={'path':str(path),'sha256':file_hash(path),'detected':kind,
            'visual_verified':False,'warnings':[]}
    if path.suffix.lower().lstrip('.')!=kind:
        result['warnings'].append('Extension is not sufficient to establish file format')
    if kind=='pdf':
        import fitz
        with fitz.open(path) as doc:
            if doc.needs_pass: raise ValueError('Encrypted PDF: provide authorized decryption separately')
            result.update(pages=len(doc),text='\n'.join(p.get_text() for p in doc),fonts=sorted({f[3] for p in doc for f in p.get_fonts()}))
    elif kind in {'docx','pptx','hwpx'}:
        from lxml import etree
        parts=zip_parts(path); nodes=[]; objects=[]; structure={}; refs=[]
        for name,data in parts.items():
            if not name.endswith(('.xml','.rels')): continue
            root=_xml(data)
            structure[name]=len(list(root.iter()))
            inspect_part=(kind=='docx' and name.startswith('word/')) or (kind=='pptx' and (re.match(r'ppt/(slides|notesSlides)/',name) or name.startswith('ppt/charts/'))) or (kind=='hwpx' and re.fullmatch(r'Contents/section\d+\.xml',name))
            if inspect_part:
                for index,node in enumerate(root.iter()):
                    local=etree.QName(node).localname if isinstance(node.tag,str) else ''
                    if local=='t' and node.text:
                        nodes.append({'part':name,'node':index,'text':node.text})
                    object_type='text' if local=='t' and node.text else None;editable=True;reason='Native package element'
                    if kind=='pptx':
                        if local=='sp':object_type='shape'
                        elif local=='tbl':object_type='table'
                        elif name.startswith('ppt/charts/') and index==0:object_type='chart'
                        elif local=='pic':object_type='image';editable=True;reason='Native image element is replaceable; pictured semantic content remains rasterized'
                        elif local=='oleObj':object_type='ole';editable=False;reason='Embedded OLE object needs its owning application and remains unverified'
                    elif kind=='docx':
                        if local=='p':object_type='paragraph'
                        elif local=='tbl':object_type='table'
                        elif local in {'oMath','oMathPara'}:object_type='equation'
                        elif local in {'drawing','pict'}:object_type='image';editable=True;reason='Native drawing element is replaceable; internal content needs a type-specific editor'
                    elif kind=='hwpx':
                        if local in {'p','paragraph'}:object_type='paragraph'
                        elif local in {'tbl','table'}:object_type='table'
                        elif local in {'equation','eqEdit','script'}:object_type='equation'
                        elif local in {'pic','img','image'}:object_type='image';editable=True;reason='Native image element is replaceable; pictured semantic content remains rasterized'
                    if object_type:
                        objects.append({'part':name,'element_id':f'{name}#node-{index}','type':object_type,
                                        'editable':editable,'reason':reason,'evidence':{'xml_local_name':local,'node':index}})
            if name.endswith('.rels'):
                for node in root:
                    if node.get('TargetMode')=='External':refs.append(node.get('Target'))
        result.update(text='\n'.join(n['text'] for n in nodes),text_nodes=nodes,objects=objects,
            package_parts=len(parts),part_hashes={k:hashlib.sha256(v).hexdigest() for k,v in parts.items()},
            xml_node_counts=structure,external_links=refs,
            slides=len([p for p in parts if re.fullmatch('ppt/slides/slide[0-9]+.xml',p)]) if kind=='pptx' else None)
        result['warnings'] += ['XML validity does not establish pagination or layout fidelity',
            'External links/macros/embedded objects are inventoried, not executed']
    else:
        result['warnings'].append('Binary HWP: use installed hwpkit for bounded edits; native Hancom for authoritative rendering')
    if 'text' in result:
        text=result['text'];result['chars_with_whitespace']=len(text)
        result['chars_without_whitespace']=len(re.sub(r'\s','',text))
        if '\ufffd' in text:result['warnings'].append('Replacement character detected in extracted text')
    return result


def edit_text(source: Path,out: Path,edits:list[dict]) -> dict:
    """Exact text-node edits. Cross-run replacements intentionally require an editor.
    Payload: [{part,node,before,after}]. All anchors checked before any output exists.
    Unchanged ZIP members retain identical uncompressed bytes, compression and names.
    """
    source,out=_new_output(source,out);kind=detect(source)
    if kind not in {'docx','pptx','hwpx'}:raise ValueError('ZIP text editor only supports DOCX/PPTX/HWPX')
    if out.suffix.lower()!='.'+kind:raise ValueError('Output extension must match actual format')
    if not isinstance(edits,list) or not 1<=len(edits)<=200:raise ValueError('Need 1..200 explicit edits')
    from lxml import etree
    original_hash=file_hash(source);parts=zip_parts(source);modified={};seen=set()
    for edit in edits:
        if set(edit)!={'part','node','before','after'} or not isinstance(edit['node'],int):raise ValueError('Invalid edit schema')
        name=edit['part']; anchor=(name,edit['node'])
        if anchor in seen:raise ValueError('Duplicate edit anchor')
        seen.add(anchor)
        if name not in parts or not name.endswith('.xml'):raise ValueError('Missing XML part')
        root=modified.setdefault(name,_xml(parts[name]));nodes=list(root.iter())
        if not 0<=edit['node']<len(nodes):raise ValueError('Node index outside document')
        node=nodes[edit['node']]
        if not isinstance(node.tag,str) or etree.QName(node).localname!='t' or node.text!=edit['before']:
            raise ValueError('Exact text anchor changed/missing; do not fall back to replacing another paragraph')
        if not isinstance(edit['after'],str) or len(edit['after'])>50000:raise ValueError('Replacement too large')
        node.text=edit['after']
        if kind in {'docx','pptx'} and (edit['after'].startswith(' ') or edit['after'].endswith(' ')):
            node.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
    replaced={name:etree.tostring(root,encoding='UTF-8',xml_declaration=parts[name].lstrip().startswith(b'<?xml')) for name,root in modified.items()}
    temp=out.with_name(out.name+'.'+str(time.time_ns())+'.tmp')
    try:
        with zipfile.ZipFile(source) as zi,zipfile.ZipFile(temp,'w') as zo:
            zo.comment=zi.comment
            for info in zi.infolist():zo.writestr(info,replaced.get(info.filename,zi.read(info)))
        for data in replaced.values():_xml(data)
        changed=zip_parts(temp)
        if set(changed)!=set(parts) or any(changed[k]!=v for k,v in parts.items() if k not in replaced):
            raise ValueError('Unexpected package mutation')
        if file_hash(source)!=original_hash:raise ValueError('Source changed concurrently')
        if out.exists():raise ValueError('Output appeared concurrently')
        # Exclusive publication; never replace a path created by another process.
        with out.open('xb') as f, temp.open('rb') as g:shutil.copyfileobj(g,f)
    finally:temp.unlink(missing_ok=True)
    receipt={'source':str(source),'source_sha256':original_hash,'output':str(out),
      'output_sha256':file_hash(out),'changed_parts':list(replaced),
      'unchanged_parts':len(parts)-len(replaced),'source_preserved':file_hash(source)==original_hash,
      'visual_verified':False,'next':'Render final document, inspect EVERY page, then independently review'}
    atomic_json(out.with_suffix(out.suffix+'.edit-receipt.json'),receipt)
    return receipt


def _pixel_record(page, image:Path, page_number:int) -> dict:
    import fitz
    pix=page.get_pixmap(matrix=fitz.Matrix(1.35,1.35),colorspace=fitz.csRGB,alpha=False)
    pix.save(image)
    return {'page':page_number,'image':str(image.resolve()),'sha256':file_hash(image),
            'pixel_sha256':hashlib.sha256(pix.samples).hexdigest(),'width':pix.width,'height':pix.height,
            'stride':pix.stride,'channels':pix.n,'text_chars':len(page.get_text()),'visual_review':'pending'}


def render_document(source:Path,out_dir:Path,backend:str='auto',timeout:int=120) -> dict:
    """PDF or LibreOffice renderer; Hancom export is explicit Windows-only.
    Isolated profile, no attachment macros. Do not run untrusted office files
    outside the user's configured sandbox; this function is not itself a sandbox.
    """
    source=source.resolve();out_dir=out_dir.absolute()
    if out_dir.exists():raise ValueError('Render into a NEW directory to avoid stale-page evidence')
    kind=detect(source);before=file_hash(source)
    out_dir.mkdir(parents=True)
    if kind=='pdf':pdf=source;engine='pymupdf'
    elif backend=='hancom':
        pdf=out_dir/(source.stem+'.pdf');export_hancom(source,pdf);engine='hancom-native'
    elif kind in {'docx','pptx'}:
        binary=shutil.which('soffice') or shutil.which('libreoffice')
        if not binary:raise ValueError('LibreOffice not installed; use an existing document skill/native Office exporter')
        with tempfile.TemporaryDirectory(prefix='apzn-lo-') as profile:
            proc=subprocess.run([binary,'-env:UserInstallation='+Path(profile).as_uri(),'--headless','--convert-to','pdf','--outdir',str(out_dir),str(source)],capture_output=True,text=True,timeout=timeout)
        pdf=out_dir/(source.stem+'.pdf');engine='libreoffice'
        if proc.returncode!=0 or not pdf.is_file():raise ValueError('Office PDF export failed: '+(proc.stderr or proc.stdout)[-2000:])
    else:raise ValueError('HWP/HWPX need a verified native renderer; XML inspection alone cannot certify page layout')
    import fitz
    pages=[]
    with fitz.open(pdf) as doc:
        if doc.needs_pass:raise ValueError('Encrypted PDF needs authorized decryption before rendering')
        if len(doc)<1:raise ValueError('Empty PDF cannot establish document evidence')
        if len(doc)>200:raise ValueError('More than 200 pages: split authorized review into explicit batches')
        for n,page in enumerate(doc,1):
            p=out_dir/f'page-{n:03d}.png';row=_pixel_record(page,p,n);row['rotation']=page.rotation;pages.append(row)
    if file_hash(source)!=before:raise ValueError('Source changed during rendering')
    manifest={'schema':2,'source':str(source),'source_sha256':before,'pdf':str(pdf.resolve()),
              'pdf_sha256':file_hash(pdf),'renderer':engine,'pages':pages,'page_count':len(pages),
              'renderer_version':getattr(fitz,'VersionBind',None),
              'render_settings':{'scale':1.35,'dpi':97.2,'colorspace':'DeviceRGB','alpha':False},
              'visual_verified':False,'rendered_at':time.time(),
              'note':'Reviewer must open every page; generated images are NOT a visual approval'}
    atomic_json(out_dir/'render.json',manifest)
    return manifest


def _absolute_file(value, label:str) -> Path:
    if not isinstance(value,str) or not value or not Path(value).is_absolute():raise ValueError(label+' path must be absolute')
    path=Path(value).resolve()
    if not path.is_file():raise ValueError(label+' file is missing')
    return path


def validate_render(manifest:dict,source:Path,*,max_pages:int|None=None) -> dict:
    source=source.resolve();source_before=file_hash(source)
    if source!=_absolute_file(manifest.get('source',''),'Render source') or source_before!=manifest.get('source_sha256'):
        raise ValueError('Render does not match final source')
    pdf=_absolute_file(manifest.get('pdf',''),'Rendered PDF');pdf_before=file_hash(pdf)
    if pdf_before!=manifest.get('pdf_sha256'):raise ValueError('Rendered PDF changed')
    if detect(source)=='pdf' and (pdf!=source or pdf_before!=source_before):raise ValueError('PDF evidence must render the exact final PDF')
    pages=manifest.get('pages',[])
    try:
        import fitz
    except ImportError as e:raise ValueError('PyMuPDF is required to verify actual PDF page evidence') from e
    schema=manifest.get('schema',1)
    if schema>=2 and manifest.get('render_settings')!={'scale':1.35,'dpi':97.2,'colorspace':'DeviceRGB','alpha':False}:
        raise ValueError('Unsupported or incomplete render settings')
    with fitz.open(pdf) as doc:
        actual_count=len(doc)
        if doc.needs_pass:raise ValueError('Encrypted PDF needs authorized decryption before validation')
        if actual_count<1:raise ValueError('Empty PDF cannot establish document evidence')
        if manifest.get('page_count')!=actual_count or len(pages)!=actual_count or [p.get('page') for p in pages]!=list(range(1,actual_count+1)):
            raise ValueError('PDF page count differs from complete render evidence')
        if max_pages is not None and actual_count>max_pages:raise ValueError('Requested page limit exceeded')
        for row,page in zip(pages,doc):
            image=_absolute_file(row.get('image',''),'Rendered page image')
            image_before=file_hash(image)
            if image_before!=row.get('sha256'):raise ValueError('Missing/changed page image')
            if schema>=2:
                pix=page.get_pixmap(matrix=fitz.Matrix(1.35,1.35),colorspace=fitz.csRGB,alpha=False)
                try:recorded=fitz.Pixmap(str(image))
                except Exception as e:raise ValueError('Rendered page image cannot be decoded') from e
                if (row.get('pixel_sha256')!=hashlib.sha256(pix.samples).hexdigest() or row.get('width')!=pix.width or
                        row.get('height')!=pix.height or row.get('stride')!=pix.stride or row.get('channels')!=pix.n or
                        row.get('rotation')!=page.rotation or recorded.width!=pix.width or recorded.height!=pix.height or
                        recorded.n!=pix.n or recorded.samples!=pix.samples):
                    raise ValueError('Page image pixels do not derive from the recorded PDF page')
            if file_hash(image)!=image_before:raise ValueError('Page image changed during validation')
    if file_hash(source)!=source_before or file_hash(pdf)!=pdf_before:raise ValueError('Source or PDF changed during validation')
    return {'actual_page_count':actual_count,'pages':list(range(1,actual_count+1)),
            'derivation_verified':schema>=2,'schema':schema,'pdf_sha256':pdf_before,'source_sha256':source_before}


def export_hancom(source:Path,out:Path) -> dict:
    source,out=_new_output(source,out)
    if os.name!='nt':raise ValueError('Hancom native export requires Windows + installed Hancom + pywin32')
    import win32com.client
    # New COM instance; never attach to a user's unsaved active document.
    hwp=win32com.client.DispatchEx('HWPFrame.HwpObject')
    try:
        if not hwp.Open(str(source)):raise ValueError('Hancom could not open the document; retain native security prompts')
        if not hwp.SaveAs(str(out),'PDF',''):raise ValueError('Hancom PDF export failed')
    finally:hwp.Quit()
    if not out.is_file() or detect(out)!='pdf':raise ValueError('Native export did not produce a valid PDF signature')
    return {'output':str(out),'sha256':file_hash(out),'native_integration_tested_here':False}


def edit_binary_hwp(source:Path,out:Path,edits:list[dict]) -> dict:
    """Conservative hwpkit >=1.0 adapter: exact unique paragraph occurrence,
    equal UTF-16 byte lengths, save/reopen verification. General length-changing
    HWP edits require a native Hancom workflow instead of this bounded adapter.
    """
    source,out=_new_output(source,out)
    if source.suffix.lower()!='.hwp' or out.suffix.lower()!='.hwp' or detect(source)!='cfb_binary_requires_native_or_hwpkit':
        raise ValueError('Binary HWP adapter requires an original .hwp CFB container; no implicit conversion')
    if not importlib.util.find_spec('hwpkit'):raise ValueError('hwpkit is not installed; request approved installation or use native Hancom skill')
    from hwpkit import open_document
    doc=open_document(str(source));before=file_hash(source)
    if not isinstance(edits,list) or not 1<=len(edits)<=200:raise ValueError('Need 1..200 explicit paragraph edits')
    expected={};seen=set()
    for e in edits:
        if not isinstance(e,dict) or set(e)!={'paragraph','before','after'} or type(e['paragraph']) is not int or e['paragraph']<0:
            raise ValueError('Each edit needs paragraph integer/before/after')
        old,new=e['before'],e['after'];n=e['paragraph']
        if not isinstance(old,str) or not old or not isinstance(new,str):raise ValueError('Need nonempty exact old text and replacement string')
        if len(old.encode('utf-16-le'))!=len(new.encode('utf-16-le')):raise ValueError('Binary HWP bounded adapter requires equal UTF-16 byte lengths; use native Hancom for general editing')
        if n in seen:raise ValueError('One edit per paragraph per transaction')
        seen.add(n);text=doc.paragraph_text(n)
        if text.count(old)!=1:raise ValueError('Paragraph anchor missing or ambiguous')
        expected[n]=text.replace(old,new,1)
    for e in edits:doc.swap_in_para_text(e['paragraph'],e['before'],e['after'])
    temp=out.with_name(out.stem+'.'+str(time.time_ns())+'.tmp.hwp')
    try:
        doc.save(str(temp));reopened=open_document(str(temp))
        if any(reopened.paragraph_text(n)!=text for n,text in expected.items()):raise ValueError('Saved HWP text verification failed')
        if detect(temp)!='cfb_binary_requires_native_or_hwpkit' or file_hash(source)!=before:raise ValueError('HWP source preservation/output failed')
        with out.open('xb') as dest,temp.open('rb') as src:shutil.copyfileobj(src,dest)
    finally:temp.unlink(missing_ok=True)
    return {'output':str(out),'source_preserved':True,'source_sha256':before,'output_sha256':file_hash(out),
            'backend':'hwpkit','visual_verified':False,'verified_paragraphs':sorted(expected),
            'warning':'Native Hancom round-trip/render still required before format-fidelity approval'}


def capabilities() -> dict:
    return {'lxml':bool(importlib.util.find_spec('lxml')),'pymupdf':bool(importlib.util.find_spec('fitz')),
            'libreoffice':shutil.which('soffice') or shutil.which('libreoffice'),
            'hwpkit':bool(importlib.util.find_spec('hwpkit')),'windows':os.name=='nt',
            'hancom_com':'not probed (opening a native app is an explicit action)',
            'ooxml_page_layout':'requires rendering, not XML estimates'}
