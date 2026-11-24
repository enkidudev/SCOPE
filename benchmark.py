"""Seeded replay benchmarks with raw, codec and equivalent F0-wrapper baselines.

All workloads are synthetic. Measurements are local-process observations, not
Windows endpoint or network bandwidth measurements. Run from any directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
import zlib
from pathlib import Path

from demo import fixture
from engine import VERSION, Store, encode

# Independent expected projections, taken from the contract specification.
# This oracle does not call normalize(), expand(), describe() or investigate().
ORACLE_FIELDS = {
    "EC-01": ["principal", "session_id", "action"],
    "EC-02": ["process_id", "parent_id"],
    "EC-03": ["process_id", "privilege_before", "privilege_after", "timestamp"],
    "EC-04": ["process_id", "remote_ip", "direction", "timestamp"],
    "EC-05": ["event_id", "timestamp"],
}


def expected(source, cid, filters):
    fields = set(ORACLE_FIELDS[cid]) | set(filters) | {"source_id", "event_id"}
    selected = [event for event in source if all(event.get(k) == v for k, v in filters.items())]
    selected.sort(key=lambda e: (e["timestamp"], e["source_id"], e["event_id"]))
    return [{f: event[f] for f in sorted(fields)} for event in selected]


def query_suite(store, sid, events):
    observations = []
    for cid in ORACLE_FIELDS:
        for filters in ({}, {"principal": events[0]["principal"]}, {"principal": "ABSENT-PRINCIPAL"}):
            before = time.perf_counter()
            answer = store.investigate(sid, cid, filters, limit=1000)
            truth = expected(events, cid, filters)
            observations.append({"contract": cid, "filters": filters, "verdict": answer["verdict"],
                                 "correct": answer.get("answer") == truth and answer.get("total_matches") == len(truth),
                                 "latency_ms": (time.perf_counter() - before) * 1000})
    return observations


def run(n=300, trials=3):
    if not 1 <= n <= 1000 or not 1 <= trials <= 20:
        raise ValueError("Benchmark supports 1..1000 events and 1..20 trials")
    codecs = ["none", "zlib"]
    try:
        import zstandard
        codecs.append("zstd")
        zstd_version = importlib.metadata.version("zstandard")
    except ImportError:
        zstandard = None
        zstd_version = None
    results, quality = [], []
    for scenario in ("repetitive", "diverse", "entropy"):
        events = fixture(n, scenario)
        raw = encode(events)
        for codec in codecs:
            elapsed, cpu = [], []
            for _ in range(trials):
                with Store(":memory:") as store:
                    t0, c0 = time.perf_counter(), time.process_time()
                    store.ingest(events, codec=codec)
                    elapsed.append((time.perf_counter() - t0) * 1000)
                    cpu.append((time.process_time() - c0) * 1000)
            with tempfile.TemporaryDirectory() as directory:
                with Store(Path(directory) / "scope.db") as scope, Store(Path(directory) / "f0.db") as baseline:
                    record = scope.ingest(events, codec=codec)
                    retained = baseline.ingest(events, codec=codec, allowed_levels=["F0"])
                    before_scope, before_f0 = scope.stats(), baseline.stats()
                    checks = query_suite(scope, record["id"], events)
                    baseline_checks = query_suite(baseline, retained["id"], events)
                    quality.extend(checks)
                    assert all(r["correct"] for r in baseline_checks), "F0 baseline oracle mismatch"
                    after_scope, after_f0 = scope.stats(), baseline.stats()
            tracemalloc.start()
            with Store(":memory:") as store:
                store.ingest(events, codec=codec)
            traced_peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            raw_codec = len(raw) if codec == "none" else len(zlib.compress(raw, 9)) if codec == "zlib" else len(zstandard.ZstdCompressor(level=3).compress(raw))
            results.append({
                "scenario": scenario, "events": n, "seed": 173, "input_sha256": hashlib.sha256(raw).hexdigest(),
                "codec": codec, "selected_level": record["level"], "raw_json_bytes": len(raw),
                "raw_codec_bytes": raw_codec, "f0_document_bytes": retained["encoded_document_bytes"],
                "scope_document_bytes": record["encoded_document_bytes"],
                "candidate_audit_bytes": record["candidate_audit_bytes"],
                "scope_db_before_queries": before_scope["sqlite_allocated_bytes"],
                "f0_db_before_queries": before_f0["sqlite_allocated_bytes"],
                "scope_db_after_queries": after_scope["sqlite_allocated_bytes"],
                "f0_db_after_queries": after_f0["sqlite_allocated_bytes"],
                "query_summary_bytes": after_scope["query_summary_bytes"],
                "savings_vs_same_codec_f0_document": 1 - record["encoded_document_bytes"] / retained["encoded_document_bytes"],
                "ingest_wall_ms_median": statistics.median(elapsed), "ingest_wall_ms_min_max": [min(elapsed), max(elapsed)],
                "ingest_cpu_ms_median": statistics.median(cpu), "python_traced_peak_bytes_separate_run": traced_peak,
                "query_ms_median": statistics.median(c["latency_ms"] for c in checks),
                "query_checks": len(checks), "correct_query_checks": sum(c["correct"] for c in checks),
            })
    with Store(":memory:") as store:
        events = fixture(20)
        reduced = store.ingest(events, allowed_levels=["F1"])
        discarded = store.ingest(events, mandatory=[], allowed_levels=["F5"])
        insufficient = [store.investigate(reduced["id"], cid, {"raw_payload": "not-retained"}) for cid in ORACLE_FIELDS]
        insufficient += [store.investigate(discarded["id"], cid) for cid in ORACLE_FIELDS]
    determinate = [q for q in quality if q["verdict"] == "SUPPORTED"]
    summary = {
        "protected_checks": len(quality), "correct_protected_checks": sum(q["correct"] for q in quality),
        "determinate_checks": len(determinate), "incorrect_determinate_checks": sum(not q["correct"] for q in determinate),
        "expected_indeterminate_checks": len(insufficient),
        "correct_indeterminate_checks": sum(q["verdict"] == "INDETERMINATE" for q in insufficient),
    }
    summary["protected_preservation_rate"] = summary["correct_protected_checks"] / len(quality)
    summary["observed_false_answer_rate"] = summary["incorrect_determinate_checks"] / len(determinate) if determinate else None
    summary["observed_indeterminate_correctness"] = summary["correct_indeterminate_checks"] / len(insufficient)
    return {
        "version": VERSION, "data_kind": "Synthetic seeded replay, not captured Windows telemetry",
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "zlib": zlib.ZLIB_VERSION, "zstandard": zstd_version},
        "trials_per_case": trials, "cases": results, "quality": summary,
        "method": "Identical canonical inputs, codec settings and 15 protected queries for optimized and forced-F0 stores",
        "limitations": ["Only three synthetic workloads", "No Windows endpoint, network or write-I/O measurements",
                        "CPU time is this Python process only; traced allocation is not RSS",
                        "Wall and CPU runs do not enable tracemalloc; traced memory is measured separately",
                        "Codec-only byte counts omit descriptors and SQLite overhead; F0-wrapper comparisons include them",
                        "Rates describe these fixed test queries only, not future or arbitrary investigations",
                        "Same-codec optimality covers the generated F0/F1/F2/F4/F5 candidates only"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=300)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", default=str(Path(__file__).parent / "data" / "benchmark.json"))
    args = parser.parse_args()
    result = run(args.events, args.trials)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    columns = [k for k, v in result["cases"][0].items() if not isinstance(v, (list, dict))]
    with output.with_suffix(".csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader(); writer.writerows(result["cases"])
    print(json.dumps(result["quality"], indent=2))
    print("Wrote", output)


if __name__ == "__main__":
    main()
