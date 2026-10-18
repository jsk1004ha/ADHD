from pathlib import Path
import tempfile
import unittest

from adhd.experience_storage import _archive_engine
from adhd.maintenance_budget import BudgetExhausted, MaintenanceBudget
from adhd.obsidian_assets.evidence import evidence_codec as codec
from tests import test_native as native_fixture


class ArchiveBudgetTests(unittest.TestCase):
    def test_hash_budget_exhaustion_preserves_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            raw = root / 'stdout.log'
            raw.write_bytes(b'x' * 4096)
            with self.assertRaises(BudgetExhausted):
                codec.store_log(root, raw, budget=MaintenanceBudget(read_bytes=1))
            self.assertEqual(b'x' * 4096, raw.read_bytes())
            self.assertEqual([raw], list(root.iterdir()))

    def test_compression_budget_exhaustion_never_publishes_partial_gzip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            raw = root / 'stdout.log'
            payload = b'x' * 4096
            raw.write_bytes(payload)
            with self.assertRaises(BudgetExhausted):
                codec.store_log(root, raw, budget=MaintenanceBudget(read_bytes=len(payload) + 1))
            self.assertEqual(payload, raw.read_bytes())
            self.assertEqual([raw], list(root.iterdir()))

    def test_codec_keeps_exact_existing_descriptor_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            raw = root / 'stdout.log'
            payload = b'exact log\r\n' * 4096
            raw.write_bytes(payload)
            budget = MaintenanceBudget()
            descriptor = codec.store_log(root, raw, budget=budget)
            self.assertEqual({'path', 'codec', 'original_sha256', 'original_bytes',
                              'stored_sha256', 'stored_bytes'}, set(descriptor))
            self.assertEqual('gzip', descriptor['codec'])
            self.assertGreaterEqual(budget.read_bytes, 2 * len(payload))
            self.assertEqual(payload, codec.read_log(root, descriptor))
            self.assertEqual(payload, raw.read_bytes())

    def test_archive_failure_cleans_owned_copy_and_preserves_native_evidence(self):
        fixture = native_fixture.NativeTests()
        fixture.setUp()
        try:
            fixture.begin()
            self.assertTrue(fixture.candidate()['ok'])
            receipt = fixture.state()['candidate']['criterion_results'][0]['evidence_ids'][0]
            fixture.spawn()
            fixture.review()
            self.assertEqual('complete', fixture.state()['status'])
            original = fixture.ws / Path(receipt).parent / 'check.stdout.jsonl'
            before = original.read_bytes()
            engine = _archive_engine()
            with self.assertRaises(BudgetExhausted):
                engine.archive_receipts(fixture.ws, fixture.state()['run_id'], [receipt],
                                        budget=MaintenanceBudget(read_bytes=0))
            self.assertEqual(before, original.read_bytes())
            archive_root = fixture.ws / '.adhd/evidence-archives'
            self.assertFalse(archive_root.exists() and any(p.is_dir() for p in archive_root.iterdir()))
            result = engine.archive_receipts(fixture.ws, fixture.state()['run_id'], [receipt])
            self.assertTrue(engine.verify_archive(fixture.ws, result['manifest'])['verified'])
            self.assertEqual(before, original.read_bytes())
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
