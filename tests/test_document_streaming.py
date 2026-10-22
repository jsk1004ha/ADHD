import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from adhd.documents import detect,inspect_document


class StreamingDocumentTests(unittest.TestCase):
    def test_media_hash_and_text_preserved_without_whole_media_read(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.hwpx'
            data=b'picture'*(512*1024)
            with zipfile.ZipFile(source,'w') as z:
                z.writestr('mimetype',b'application/hwp+zip')
                z.writestr('Contents/section0.xml',b'<section><p><t>kept</t></p></section>')
                z.writestr('BinData/picture.bin',data)
            original_read=zipfile.ZipFile.read
            def bounded_read(archive,name,*args,**kwargs):
                label=name.filename if hasattr(name,'filename') else name
                self.assertNotEqual(label,'BinData/picture.bin')
                return original_read(archive,name,*args,**kwargs)
            with patch.object(zipfile.ZipFile,'read',bounded_read):
                self.assertEqual(detect(source),'hwpx')
                report=inspect_document(source)
            self.assertEqual(report['text'],'kept')
            self.assertEqual(report['part_hashes']['BinData/picture.bin'],hashlib.sha256(data).hexdigest())
            self.assertEqual(report['package_parts'],3)

    def test_unselected_unsafe_member_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.hwpx'
            with zipfile.ZipFile(source,'w') as z:
                z.writestr('Contents/section0.xml','<section/>')
                z.writestr('../escape',b'unsafe')
            with self.assertRaisesRegex(ValueError,'Unsafe'):
                inspect_document(source)
