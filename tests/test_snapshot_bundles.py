from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adhd import documents
from adhd.snapshots import SnapshotPolicy,build_snapshot,preflight_snapshot,validate_snapshot


class SnapshotBundleTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.ws=Path(self.temp.name).resolve()
    def tearDown(self):self.temp.cleanup()

    def render(self,pages=2):
        import fitz
        source=self.ws/'final.pdf';doc=fitz.open()
        for number in range(pages):doc.new_page(width=72,height=72).insert_text((5,20),str(number+1))
        doc.save(source);doc.close();documents.render_document(source,self.ws/'render');return source

    def test_large_binary_is_streamed(self):
        path=self.ws/'large.bin';path.write_bytes(b'x'*(3*1024*1024))
        with patch.object(Path,'read_bytes',side_effect=AssertionError('must stream')):
            snap=build_snapshot(self.ws,['large.bin'],policy={'max_file_bytes':4*1024*1024})
        self.assertEqual(snap['artifacts'][0]['size'],3*1024*1024)

    def test_huge_json_is_not_loaded_as_control_json(self):
        path=self.ws/'huge.json';path.write_bytes(b' '*1025)
        with self.assertRaisesRegex(ValueError,'size limit'):
            build_snapshot(self.ws,['huge.json'],policy={'max_control_json_bytes':1024})

    def test_two_hundred_page_bundle_fits_policy(self):
        self.render(200)
        snap=build_snapshot(self.ws,[{'kind':'render_bundle','manifest':'render/render.json'}])
        self.assertEqual(snap['artifacts'][0]['page_count'],200)
        self.assertEqual(len(snap['artifacts'][0]['leaves']),201)

    def test_one_leaf_changed_invalidates_candidate(self):
        self.render();snap=build_snapshot(self.ws,[{'kind':'render_bundle','manifest':'render/render.json'}])
        (self.ws/'render/page-002.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):validate_snapshot(self.ws,snap)

    def test_duplicate_or_escaping_leaf_rejected(self):
        self.render();path=self.ws/'render/render.json';manifest=json.loads(path.read_text())
        manifest['pages'][1]['image']=manifest['pages'][0]['image'];manifest['pages'][1]['sha256']=manifest['pages'][0]['sha256']
        path.write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Duplicate'):build_snapshot(self.ws,[{'kind':'render_bundle','manifest':'render/render.json'}])
        manifest['pages'][1]['image']=str((self.ws.parent/'outside.png').resolve());path.write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'workspace'):build_snapshot(self.ws,[{'kind':'render_bundle','manifest':'render/render.json'}])

    def test_preflight_reports_total_budget(self):
        result=preflight_snapshot([{'files':202,'bytes':900}],policy={'max_bundle_files':202,'max_total_bytes':1000})
        self.assertEqual(result['remaining_bytes'],100)
        with self.assertRaisesRegex(ValueError,'byte total'):preflight_snapshot([{'files':1,'bytes':1001}],policy={'max_total_bytes':1000})

    def test_validation_timeout_is_unverified(self):
        path=self.ws/'a.bin';path.write_bytes(b'x')
        with patch('adhd.snapshots.time.monotonic',side_effect=[100.0,100.0,102.0]):
            with self.assertRaisesRegex(TimeoutError,'unverified'):
                build_snapshot(self.ws,['a.bin'],policy={'max_validation_seconds':1.0})


if __name__=='__main__':unittest.main()
