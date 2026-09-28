from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from adhd import documents
from adhd.gates import document_candidate,validate_document_contract
from adhd.native import checked_path


class EditableElementTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.ws=Path(self.temp.name).resolve()
    def tearDown(self):self.temp.cleanup()

    def package(self,name,slide):
        path=self.ws/name
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('ppt/presentation.xml','<p:presentation xmlns:p="urn:p"/>')
            archive.writestr('ppt/slides/slide1.xml',slide)
        return path

    def evidence(self,source,contract):
        import fitz
        pdf=self.ws/'rendered.pdf';doc=fitz.open();doc.new_page().insert_text((20,20),'render');doc.save(pdf);doc.close()
        render=documents.render_document(pdf,self.ws/'render')
        render['source']=str(source.resolve());render['source_sha256']=documents.file_hash(source)
        (self.ws/'render/render.json').write_text(json.dumps(render),encoding='utf-8')
        state={'workspace':str(self.ws),'contract':{'artifacts':[source.name],'documents':[{'path':source.name,**contract}]}}
        return document_candidate(state,{'document_evidence':[{'path':source.name,'render_manifest':'render/render.json'}]},checked_path)

    def test_rasterized_chart_with_decoy_text_rejected(self):
        source=self.package('chart.pptx','<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:sp><a:t>Chart</a:t></p:sp><p:pic/></p:sld>')
        with self.assertRaisesRegex(ValueError,'Required editable element'):
            self.evidence(source,{'editable_elements':[{'selector':'*','type':'chart','required':True}]})

    def test_native_table_is_identified_and_accepted(self):
        source=self.package('table.pptx','<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:tbl><a:t>cell</a:t></a:tbl></p:sld>')
        evidence,_=self.evidence(source,{'editable_elements':[{'selector':'*','type':'table','required':True}]})
        self.assertTrue(evidence[0]['editable_elements'][0]['matched'])

    def test_unsupported_object_reported_uneditable(self):
        source=self.package('ole.pptx','<p:sld xmlns:p="urn:p"><p:oleObj/></p:sld>')
        objects=documents.inspect_document(source)['objects']
        self.assertEqual(objects[0]['type'],'ole');self.assertFalse(objects[0]['editable']);self.assertIn('unverified',objects[0]['reason'])

    def test_contract_rejects_unbounded_or_unknown_element(self):
        with self.assertRaises(ValueError):
            validate_document_contract([{'path':'a.pptx','editable_elements':[{'selector':'*','type':'video','required':True}]}],['a.pptx'],self.ws,checked_path)

    def test_layout_checks_are_bound_into_evidence(self):
        source=self.package('layout.pptx','<p:sld xmlns:p="urn:p"><p:sp/></p:sld>')
        evidence,_=self.evidence(source,{'layout_checks':['No clipped slide text','No overflow']})
        self.assertEqual(evidence[0]['layout_checks'],['No clipped slide text','No overflow'])


if __name__=='__main__':unittest.main()
