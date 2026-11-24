import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo import fixture
from engine import CONTRACTS, IntegrityError, Store, canonical_time, candidates, describe, encode, expand, normalize


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.db.close()

    def test_all_contract_facts_match_input(self):
        events = fixture(20)
        record = self.store.ingest(events)
        for cid, contract in CONTRACTS.items():
            result = self.store.investigate(record["id"], cid)
            self.assertEqual(result["verdict"], "SUPPORTED")
            self.assertEqual(result["total_matches"], 20)
            self.assertEqual(result["answer"][0], {f: events[0][f] for f in set(contract["fields"]) | {"source_id", "event_id"}})

    def test_mandatory_missing_rejected_without_insert(self):
        events = fixture(2); del events[0]["session_id"]
        with self.assertRaises(ValueError):
            self.store.ingest(events)
        self.assertEqual(self.store.stats()["segments"], 0)

    def test_subset_policy_allows_missing_unprotected_fields(self):
        events = fixture(2); del events[0]["session_id"]
        record = self.store.ingest(events, ["EC-05"])
        self.assertEqual(self.store.investigate(record["id"], "EC-01")["verdict"], "INDETERMINATE")
        self.assertEqual(self.store.investigate(record["id"], "EC-05")["verdict"], "SUPPORTED")

    def test_reduction_has_no_hidden_payload(self):
        record = self.store.ingest(fixture(20), allowed_levels=["F1", "F2"])
        self.assertNotIn("SYNTHETIC REPLAY PAYLOAD", json.dumps(record))
        self.assertEqual(record["field_coverage"]["raw_payload"], "UNAVAILABLE")
        self.assertEqual(self.store.investigate(record["id"], "EC-01", {"raw_payload": "x"})["verdict"], "INDETERMINATE")

    def test_discarded_segment_is_indeterminate(self):
        record = self.store.ingest(fixture(2), [])
        self.assertEqual(record["level"], "F5")
        self.assertEqual(self.store.investigate(record["id"], "EC-01")["verdict"], "INDETERMINATE")

    def test_unknown_query_audited(self):
        record = self.store.ingest(fixture(2))
        self.assertEqual(self.store.investigate(record["id"], "EC-unknown")["verdict"], "UNSUPPORTED")
        self.assertEqual(self.store.stats()["queries"], 1)

    def test_no_match_is_supported_empty(self):
        record = self.store.ingest(fixture(2))
        result = self.store.investigate(record["id"], "EC-01", {"principal": "absent"})
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertEqual(result["total_matches"], 0)
        self.assertEqual(result["answer"], [])

    def test_pagination_counts_and_continuation(self):
        record = self.store.ingest(fixture(7))
        result = self.store.investigate(record["id"], "EC-05", limit=3, offset=3)
        self.assertEqual([e["event_id"] for e in result["answer"]], ["3", "4", "5"])
        self.assertEqual((result["total_matches"], result["next_offset"]), (7, 6))
        result = self.store.investigate(record["id"], "EC-05", limit=3, offset=6)
        self.assertEqual(result["returned"], 1); self.assertIsNone(result["next_offset"])

    def test_audit_contains_summary_not_answers(self):
        record = self.store.ingest(fixture(200))
        self.store.investigate(record["id"], "EC-01", limit=1000)
        raw = self.store.db.execute("SELECT summary FROM investigations").fetchone()[0]
        self.assertLess(len(raw), 1000)
        self.assertNotIn('"answer"', raw)
        self.assertIn('"result_sha256"', raw)

    def test_swapping_attribution_rejected(self):
        events = normalize(fixture(2)); events[1]["principal"] = "different"
        candidate = candidates(events)[1]
        candidate["payload"][0]["principal"], candidate["payload"][1]["principal"] = candidate["payload"][1]["principal"], candidate["payload"][0]["principal"]
        self.assertNotEqual(describe(events, candidate)["EC-01"]["status"], "EXACT")

    def test_lost_aggregate_occurrence_rejected(self):
        events = normalize(fixture(12)); candidate = candidates(events)[2]
        candidate["payload"][0]["occurrences"].pop()
        self.assertTrue(all(d["status"] != "EXACT" for d in describe(events, candidate).values()))

    def test_mixed_aggregate_order_restored(self):
        events = normalize(fixture(20, "diverse")); candidate = candidates(events)[2]
        self.assertEqual([e["event_id"] for e in expand(candidate)], [e["event_id"] for e in events])

    def test_same_codec_minimum(self):
        for codec in ("none", "zlib"):
            record = self.store.ingest(fixture(30), codec=codec)
            admissible = [c["encoded_bytes"] for c in record["candidates"] if c["admissible"]]
            self.assertEqual(record["encoded_document_bytes"], min(admissible))

    def test_full_retention_keeps_unprotected_data(self):
        record = self.store.ingest(fixture(2), allowed_levels=["F0"])
        result = self.store.investigate(record["id"], "EC-01", {"raw_payload": fixture(2)[0]["raw_payload"]})
        self.assertEqual(result["total_matches"], 2)

    def test_commitment_match_and_mismatch(self):
        events = fixture(2); record = self.store.ingest(events, [], allowed_levels=["F4"])
        self.assertTrue(self.store.verify_original(record["id"], events)["matches"])
        events[0]["principal"] = "changed"
        self.assertFalse(self.store.verify_original(record["id"], events)["matches"])
        self.assertEqual(self.store.investigate(record["id"], "EC-01")["verdict"], "INDETERMINATE")

    def test_commitment_invalid_state_is_audited(self):
        record = self.store.ingest(fixture(2))
        self.assertEqual(self.store.verify_original(record["id"], fixture(2))["verdict"], "UNSUPPORTED")
        self.store.db.execute("UPDATE segments SET document=? WHERE id=?", (b"bad", record["id"]))
        self.assertEqual(self.store.verify_original(record["id"], fixture(2))["verdict"], "INDETERMINATE")
        self.assertEqual(self.store.stats()["queries"], 2)

    def test_forced_discard_cannot_override_mandatory(self):
        with self.assertRaises(ValueError):
            self.store.ingest(fixture(2), allowed_levels=["F5"])

    def test_duplicate_id_rejected_but_other_source_allowed(self):
        events = fixture(2); events[1]["event_id"] = events[0]["event_id"]
        with self.assertRaises(ValueError):
            normalize(events)
        events[1]["source_id"] = "other"
        self.assertEqual(len(normalize(events)), 2)

    def test_nan_rejected(self):
        events = fixture(1); events[0]["extra"] = float("nan")
        with self.assertRaises(ValueError):
            normalize(events)

    def test_precision_and_timezone_preserved(self):
        self.assertEqual(canonical_time("2026-11-09T05:30:00.1234567+05:30"), "2026-11-09T00:00:00.123456700Z")
        self.assertEqual(canonical_time("2026-11-09T00:00:00.123456789Z"), "2026-11-09T00:00:00.123456789Z")
        for bad in ("2026-11-09T00:00:00", "2026-11-09T00:00:00.1234567891Z", "2026-11-09T00:00:00+00:99"):
            with self.assertRaises(ValueError): canonical_time(bad)

    def test_checksum_corruption_is_indeterminate(self):
        record = self.store.ingest(fixture(2))
        self.store.db.execute("UPDATE segments SET document=? WHERE id=?", (b"corrupted", record["id"]))
        self.assertEqual(self.store.investigate(record["id"], "EC-01")["verdict"], "INDETERMINATE")
        self.assertEqual(self.store.list()[0]["integrity"], "FAILED")

    def test_ancestry_reaches_explicit_boundary(self):
        record = self.store.ingest(fixture(scenario="chain"))
        result = self.store.relation(record["id"], "ancestry", "lab-win01", process_id="proc-3")
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertEqual([e["process_id"] for e in result["chain"]], ["proc-3", "proc-2", "proc-1", "proc-0"])

    def test_root_marker_is_not_an_observed_process(self):
        record = self.store.ingest(fixture(scenario="chain"))
        result = self.store.relation(record["id"], "ancestry", "missing-source", process_id="ROOT")
        self.assertEqual(result["verdict"], "INDETERMINATE")

    def test_missing_ancestor_cycle_and_conflict(self):
        for kind in ("missing", "cycle", "conflict"):
            events = fixture(scenario="chain")
            if kind == "missing": events.pop(0)
            elif kind == "cycle": events[0]["parent_id"] = "proc-3"
            else:
                row = dict(events[2], event_id="other", parent_id="different")
                events.append(row)
            record = self.store.ingest(events)
            self.assertEqual(self.store.relation(record["id"], "ancestry", "lab-win01", process_id="proc-3")["verdict"], "INDETERMINATE")

    def test_order_ties_submicrosecond_and_missing(self):
        events = fixture(2)
        events[0]["timestamp"] = "2026-11-09T00:00:00.000000001Z"
        events[1]["timestamp"] = "2026-11-09T00:00:00.000000002Z"
        record = self.store.ingest(events)
        result = self.store.relation(record["id"], "order", "lab-win01", event_a="0", event_b="1")
        self.assertEqual(result["relation"], "BEFORE")
        self.assertEqual(self.store.relation(record["id"], "order", "other", event_a="0", event_b="1")["verdict"], "INDETERMINATE")
        events[1]["timestamp"] = events[0]["timestamp"]
        record = self.store.ingest(events)
        self.assertEqual(self.store.relation(record["id"], "order", "lab-win01", event_a="0", event_b="1")["relation"], "SAME_TIMESTAMP")

    def test_invalid_policy_pagination_and_filter_types(self):
        for mandatory in ("EC-01", ["INVALID"], [1]):
            with self.assertRaises(ValueError): self.store.ingest(fixture(1), mandatory)
        record = self.store.ingest(fixture(1))
        with self.assertRaises(ValueError): self.store.investigate(record["id"], "EC-01", limit=0)
        with self.assertRaises(ValueError): self.store.investigate(record["id"], "EC-01", {"a": 1})

    def test_policy_snapshot_survives_current_definition_change(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "e.db"; policy = Path(directory) / "policy.json"
            with Store(db) as original:
                record = original.ingest(fixture(2))
            policy.write_text(json.dumps({"policy_version": "changed", "contracts": {"EC-01": {"name": "New meaning", "fields": ["new_field"]}}}))
            with Store(db, policy) as changed:
                self.assertEqual(changed.investigate(record["id"], "EC-01")["verdict"], "SUPPORTED")

    def test_backup_and_schema_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "backup.db"
            record = self.store.ingest(fixture(1)); self.store.backup(file)
            with Store(file) as copy:
                self.assertEqual(copy.get(record["id"])["level"], record["level"])
            with self.assertRaises(ValueError): self.store.backup(file)
            old = Path(directory) / "old.db"
            with sqlite3.connect(old) as connection:
                connection.execute("CREATE TABLE segments(id INTEGER PRIMARY KEY, document TEXT)")
            with self.assertRaises(ValueError): Store(old)


if __name__ == "__main__": unittest.main()
