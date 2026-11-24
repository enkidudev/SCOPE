"""Regression checks for the 0.2.1 review refinements."""
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo import fixture
from engine import IntegrityError, Store, encode, unpack


class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.db.close()

    def test_missing_zstd_returns_indeterminate_and_listing_survives(self):
        r = self.store.ingest(fixture(2))
        self.store.db.execute("UPDATE segments SET codec='zstd' WHERE id=?", (r['id'],))
        with patch.dict(sys.modules, {'zstandard': None}):
            result = self.store.investigate(r['id'], 'EC-01')
            self.assertEqual(result['verdict'], 'INDETERMINATE')
            self.assertIn('install requirements-benchmark.txt', result['reason'])
            self.assertEqual(self.store.list()[0]['integrity'], 'FAILED')

    def test_bad_zstd_frame_returns_controlled_integrity_error(self):
        class DecoderError(Exception): pass
        class Decoder:
            def decompress(self, *args, **kwargs): raise DecoderError('invalid frame')
        fake = SimpleNamespace(ZstdError=DecoderError, ZstdDecompressor=Decoder)
        with patch.dict(sys.modules, {'zstandard': fake}):
            with self.assertRaisesRegex(IntegrityError, 'cannot be decoded'):
                unpack(b'bad frame', 'zstd')

    def test_previous_patch_document_remains_readable(self):
        r = self.store.ingest(fixture(2), allowed_levels=['F0'])
        blob = self.store.db.execute('SELECT document FROM segments WHERE id=?', (r['id'],)).fetchone()[0]
        document = json.loads(blob); document['schema_version'] = '0.2.0'
        old = encode(document)
        self.store.db.execute('UPDATE segments SET document=?,sha256=? WHERE id=?', (old, hashlib.sha256(old).hexdigest(), r['id']))
        self.assertEqual(self.store.investigate(r['id'], 'EC-01')['total_matches'], 2)

    def test_future_document_version_is_rejected(self):
        r = self.store.ingest(fixture(1), allowed_levels=['F0'])
        blob = self.store.db.execute('SELECT document FROM segments').fetchone()[0]
        document = json.loads(blob); document['schema_version'] = '9.0.0'
        blob = encode(document)
        self.store.db.execute('UPDATE segments SET document=?,sha256=?', (blob, hashlib.sha256(blob).hexdigest()))
        self.assertEqual(self.store.investigate(r['id'], 'EC-01')['verdict'], 'INDETERMINATE')

    def test_corrupt_audit_does_not_escape_as_json_or_type_error(self):
        r = self.store.ingest(fixture(2))
        for bad in ('{broken', '{}', 'null'):
            self.store.db.execute('UPDATE segments SET candidate_audit=?', (bad,))
            self.assertEqual(self.store.investigate(r['id'], 'EC-01')['verdict'], 'INDETERMINATE')
            self.assertEqual(self.store.list()[0]['integrity'], 'FAILED')

    def test_audit_storage_counts_utf8_bytes(self):
        r = self.store.ingest(fixture(2))
        self.store.investigate(r['id'], 'EC-01', {'principal': '用户'})
        summary = self.store.db.execute('SELECT summary FROM investigations').fetchone()[0]
        self.assertGreater(len(summary.encode('utf-8')), len(summary))
        self.assertEqual(self.store.stats()['query_summary_bytes'], len(summary.encode('utf-8')))

    def test_candidate_audit_storage_counts_utf8_bytes(self):
        self.store.ingest(fixture(1))
        audit = '[{"review":"证据"}]'
        self.store.db.execute('UPDATE segments SET candidate_audit=?', (audit,))
        self.assertEqual(self.store.stats()['candidate_audit_bytes'], len(audit.encode('utf-8')))
