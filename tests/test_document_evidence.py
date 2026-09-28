from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adhd import documents
from adhd.core import file_hash
from adhd.gates import document_candidate
from adhd.native import checked_path


class DocumentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ws=Path(self.temp.name).resolve()

    def tearDown(self):self.temp.cleanup()

    def pdf(self,name='final.pdf',pages=2):
        import fitz
        path=self.ws/name;doc=fitz.open()
        for number in range(1,pages+1):doc.new_page().insert_text((30,30),f'page {number}')
        doc.save(path);doc.close();return path

    def render(self,pages=2):
        source=self.pdf(pages=pages);return source,documents.render_document(source,self.ws/'render')

    def candidate(self,source,manifest,contract):
        state={'workspace':str(self.ws),'contract':{'artifacts':[source.name],'documents':[{'path':source.name,**contract}]}}
        return document_candidate(state,{'document_evidence':[{'path':source.name,'render_manifest':'render/render.json'}]},checked_path)

    def test_truncated_manifest_rejects_two_page_pdf(self):
        source,manifest=self.render();manifest['pages']=manifest['pages'][:1];manifest['page_count']=1
        with self.assertRaisesRegex(ValueError,'page count'):documents.validate_render(manifest,source)

    def test_replaced_pdf_rejects_even_with_updated_hash(self):
        source,manifest=self.render();replacement=self.pdf('replacement.pdf',3)
        source.write_bytes(replacement.read_bytes());fresh=file_hash(source)
        manifest['source_sha256']=fresh;manifest['pdf_sha256']=fresh
        with self.assertRaisesRegex(ValueError,'page count'):documents.validate_render(manifest,source)

    def test_wrong_page_pixels_rejected(self):
        source,manifest=self.render();first=Path(manifest['pages'][0]['image']);second=Path(manifest['pages'][1]['image'])
        second.write_bytes(first.read_bytes());manifest['pages'][1]['sha256']=file_hash(second)
        manifest['pages'][1]['pixel_sha256']=manifest['pages'][0]['pixel_sha256']
        with self.assertRaisesRegex(ValueError,'pixels'):documents.validate_render(manifest,source)

    def test_exact_pages_uses_actual_pdf(self):
        source,manifest=self.render()
        with self.assertRaisesRegex(ValueError,'Exact document page count'):self.candidate(source,manifest,{'exact_pages':1})

    def test_manifest_paths_independent_of_process_cwd(self):
        source,manifest=self.render(pages=1);old=Path.cwd()
        try:
            os.chdir(self.ws.parent)
            evidence,_=self.candidate(source,manifest,{'exact_pages':1})
        finally:os.chdir(old)
        self.assertEqual(evidence[0]['pages'],[1]);self.assertTrue(evidence[0]['render_derivation_verified'])

    def test_render_file_changed_during_validation(self):
        source,manifest=self.render(pages=1);real=documents.file_hash;calls=0
        def changing(path):
            nonlocal calls
            calls+=1
            if calls==4:Path(path).write_bytes(Path(path).read_bytes()+b' ')
            return real(Path(path))
        with patch('adhd.documents.file_hash',side_effect=changing):
            with self.assertRaisesRegex(ValueError,'changed during validation'):documents.validate_render(manifest,source)

    def test_schema1_requires_regeneration_for_full_derivation(self):
        source,manifest=self.render(pages=1);legacy=copy.deepcopy(manifest);legacy['schema']=1
        self.assertFalse(documents.validate_render(legacy,source)['derivation_verified'])
        (self.ws/'render/render.json').write_text(json.dumps(legacy),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'must be regenerated'):self.candidate(source,legacy,{'exact_pages':1})

    def test_manifest_pixel_hash_cannot_hide_replaced_png(self):
        source,manifest=self.render(pages=1);image=Path(manifest['pages'][0]['image'])
        image.write_bytes(b'not a png');manifest['pages'][0]['sha256']=hashlib.sha256(b'not a png').hexdigest()
        with self.assertRaisesRegex(ValueError,'decode'):documents.validate_render(manifest,source)


if __name__=='__main__':unittest.main()
