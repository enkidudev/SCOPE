"""Contract-constrained telemetry retention, with segment-local guarantees.

The verifier proves equality of projections of supplied, normalized records.
It does not prove source truth, capture completeness or causal relationships.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.2.2"
READABLE_DOCUMENT_VERSIONS = {"0.2.0", "0.2.1", VERSION}
MAX_EVENTS = 20_000
MAX_INPUT_BYTES = 16_000_000
MAX_DOCUMENT_BYTES = 64_000_000
LEVELS = ("F0", "F1", "F2", "F4", "F5")
BASE_FIELDS = {"event_id", "source_id", "event_type", "timestamp"}
TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
SCOPE = "Submitted segment only; no guarantee of collection completeness or source truth"


class IntegrityError(ValueError):
    """A stored document failed its checksum, decoding or version check."""


def encode(value):
    """Canonical JSON bytes. NaN and Infinity are never valid evidence values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def load_policy(filename=None):
    filename = Path(filename) if filename else Path(__file__).with_name("contracts.json")
    policy = json.loads(filename.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or not isinstance(policy.get("policy_version"), str):
        raise ValueError("Policy requires a string policy_version")
    definitions = policy.get("contracts")
    if not isinstance(definitions, dict) or not definitions or len(definitions) > 64:
        raise ValueError("Policy requires 1 to 64 contract definitions")
    for cid, contract in definitions.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", cid) or not isinstance(contract, dict):
            raise ValueError("Invalid contract definition")
        fields = contract.get("fields")
        if not isinstance(contract.get("name"), str) or not isinstance(fields, list) or not fields:
            raise ValueError("Each contract requires name and nonempty fields")
        if any(not isinstance(f, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", f) for f in fields):
            raise ValueError("Contract fields must be simple field names")
        if len(set(fields)) != len(fields):
            raise ValueError("Repeated contract fields are not allowed")
    return policy


CONTRACTS = load_policy()["contracts"]


def canonical_time(value):
    """Normalize to UTC without discarding up to nine fractional digits."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    match = TIMESTAMP.fullmatch(value)
    if not match:
        raise ValueError("Recorded time needs a date, time and timezone, with at most 9 decimal places (example: 2026-11-09T09:30:00Z)")
    day, clock, fraction, zone = match.groups()
    if zone != "Z":
        hours, minutes = map(int, zone[1:].split(":"))
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid timezone offset")
    base = datetime.fromisoformat(day + "T" + clock + ("+00:00" if zone == "Z" else zone))
    return base.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "." + (fraction or "").ljust(9, "0") + "Z"


def event_key(event):
    return event["timestamp"], event["source_id"], event["event_id"]


def normalize(events, definitions=None):
    """Validate a bounded JSON replay and preserve unknown JSON fields for F0."""
    definitions = CONTRACTS if definitions is None else definitions
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError(f"Add a group containing 1 to {MAX_EVENTS} records")
    if len(encode(events)) > MAX_INPUT_BYTES:
        raise ValueError("This group is too large. Split it into groups smaller than 16 MB")
    known = BASE_FIELDS | {f for c in definitions.values() for f in c["fields"]}
    result, seen = [], set()
    for position, row in enumerate(events):
        if not isinstance(row, dict) or any(not isinstance(k, str) for k in row):
            raise ValueError(f"Record {position + 1} must contain named details")
        event = dict(row)
        for field in BASE_FIELDS:
            if not isinstance(event.get(field), str) or not event[field]:
                raise ValueError(f"Record {position + 1} needs a nonempty text value for {field}")
        identity = event["source_id"], event["event_id"]
        if identity in seen:
            raise ValueError("Two records use the same source and record number. Give each record a unique number within its source")
        seen.add(identity)
        for field in known & event.keys():
            if not isinstance(event[field], str) or not event[field]:
                raise ValueError(f"Record {position + 1}: {field} must contain a nonempty text value")
        event["timestamp"] = canonical_time(event["timestamp"])
        result.append(event)
    return sorted(result, key=event_key)


def expand(candidate):
    """Reconstruct retained normalized facts, never discarded payload."""
    if candidate["level"] in ("F0", "F1"):
        return candidate["payload"]
    if candidate["level"] == "F2":
        events = [dict(group["values"], **occurrence)
                  for group in candidate["payload"] for occurrence in group["occurrences"]]
        return sorted(events, key=event_key)
    return []


def candidates(events, definitions=None):
    definitions = CONTRACTS if definitions is None else definitions
    fields = BASE_FIELDS | {f for c in definitions.values() for f in c["fields"]}
    reduced = [{k: v for k, v in e.items() if k in fields} for e in events]
    groups = {}
    for event in reduced:
        values = {k: v for k, v in event.items() if k not in ("event_id", "timestamp")}
        key = encode(values)
        groups.setdefault(key, {"values": values, "occurrences": []})["occurrences"].append(
            {k: event[k] for k in ("event_id", "timestamp")})
    return [
        {"level": "F0", "payload": events},
        {"level": "F1", "payload": reduced},
        {"level": "F2", "payload": list(groups.values())},
        {"level": "F4", "payload": {"sha256": hashlib.sha256(encode(events)).hexdigest(), "count": len(events)}},
        {"level": "F5", "payload": None},
    ]


def projection(events, fields):
    # Event identities bind individual facts to their original records.
    names = sorted(set(fields) | {"source_id", "event_id"})
    return Counter(encode([event.get(f) for f in names]) for event in events)


def describe(original, candidate, definitions=None):
    definitions = CONTRACTS if definitions is None else definitions
    retained, result = expand(candidate), {}
    for cid, contract in definitions.items():
        missing = sorted({f for event in original for f in contract["fields"] if f not in event})
        equal = projection(original, contract["fields"]) == projection(retained, contract["fields"])
        status = "EXACT" if not missing and equal else ("PARTIAL" if retained else "UNAVAILABLE")
        result[cid] = {
            "status": status, "required_fields": contract["fields"], "missing_source_fields": missing,
            "method": "event-bound projection multiset equality v2",
            "reason": "Kept for every supplied record" if status == "EXACT" else
                      ("Some required details were missing from the supplied records" if missing else "This storage choice removed required details"),
        }
    return result


def pack(document, codec):
    raw = encode(document)
    if codec == "none":
        return raw
    if codec == "zlib":
        return zlib.compress(raw, 9)
    if codec == "zstd":
        try:
            import zstandard
        except ImportError as exc:
            raise ValueError("zstd requires the optional zstandard package") from exc
        return zstandard.ZstdCompressor(level=3).compress(raw)
    raise ValueError("codec must be none, zlib or zstd")


def unpack(blob, codec):
    if codec == "none":
        raw = blob
    elif codec == "zlib":
        decoder = zlib.decompressobj()
        raw = decoder.decompress(blob, MAX_DOCUMENT_BYTES + 1)
        if not decoder.eof or decoder.unused_data:
            raise IntegrityError("Invalid or oversized zlib document")
    elif codec == "zstd":
        try:
            import zstandard
        except ImportError as exc:
            raise IntegrityError("Cannot read zstd evidence: install requirements-benchmark.txt") from exc
        try:
            raw = zstandard.ZstdDecompressor().decompress(blob, max_output_size=MAX_DOCUMENT_BYTES)
        except zstandard.ZstdError as exc:
            raise IntegrityError("Stored zstd evidence cannot be decoded") from exc
    else:
        raise IntegrityError("Unrecognized stored codec")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise IntegrityError("Stored document exceeds decoded limit")
    return json.loads(raw)


def policy_ids(mandatory, definitions):
    if mandatory is None:
        return sorted(definitions)
    if not isinstance(mandatory, list) or any(not isinstance(c, str) for c in mandatory):
        raise ValueError("mandatory must be an array of contract IDs")
    unknown = set(mandatory) - definitions.keys()
    if unknown:
        raise ValueError("Unknown contracts: " + ", ".join(sorted(unknown)))
    return sorted(set(mandatory))


def choose(events, mandatory, policy, codec="none", allowed_levels=None):
    """Measure each full candidate document with the same codec and policy."""
    definitions = policy["contracts"]
    mandatory = policy_ids(mandatory, definitions)
    allowed_levels = list(LEVELS) if allowed_levels is None else allowed_levels
    if not isinstance(allowed_levels, list) or not allowed_levels or any(x not in LEVELS for x in allowed_levels):
        raise ValueError("allowed_levels must be a nonempty array of F0, F1, F2, F4 or F5")
    common = {
        "schema_version": VERSION, "policy_version": policy["policy_version"], "contracts": definitions,
        "mandatory": mandatory, "source_event_count": len(events),
        "sources": sorted({e["source_id"] for e in events}),
        "time_start": events[0]["timestamp"], "time_end": events[-1]["timestamp"],
    }
    source_fields = sorted({k for event in events for k in event})
    options, admissible = [], []
    for candidate in candidates(events, definitions):
        retained = expand(candidate)
        descriptor = describe(events, candidate, definitions)
        field_coverage = {
            field: "EXACT" if all(field in e for e in events) and
            projection(events, [field]) == projection(retained, [field]) else "UNAVAILABLE"
            for field in source_fields
        }
        document = dict(common, level=candidate["level"], payload=candidate["payload"],
                        descriptor=descriptor, field_coverage=field_coverage)
        blob = pack(document, codec)
        failed = [cid for cid in mandatory if descriptor[cid]["status"] != "EXACT"]
        permitted = candidate["level"] in allowed_levels
        accepted = permitted and not failed
        options.append({"level": candidate["level"], "encoded_bytes": len(blob), "admissible": accepted,
                        "failed_contracts": failed, "allowed": permitted})
        if accepted:
            admissible.append((len(blob), LEVELS.index(candidate["level"]), document, blob))
    if not admissible:
        missing = {cid: sorted({f for e in events for f in definitions[cid]["fields"] if f not in e}) for cid in mandatory}
        raise ValueError("Cannot save this group with the selected rules. Missing details by rule: " + json.dumps(missing))
    _, _, document, blob = min(admissible, key=lambda row: row[:2])
    return document, blob, options


class Store:
    """SQLite persistence with compressed documents and bounded query summaries."""

    def __init__(self, filename="scope_v02.db", policy_file=None):
        self.policy = load_policy(policy_file)
        self.db = sqlite3.connect(str(filename), timeout=10)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        existing = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='segments'").fetchone()
        if version not in (0, 2) or (existing and version != 2):
            self.db.close()
            raise ValueError("This database format is incompatible. Create a new database and reimport the original records.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY, document BLOB NOT NULL, codec TEXT NOT NULL,
                sha256 TEXT NOT NULL, candidate_audit TEXT NOT NULL,
                input_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS investigations (
                id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, summary TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS investigations_segment ON investigations(segment_id);
            CREATE TABLE IF NOT EXISTS segment_id_sequence (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1), last_id INTEGER NOT NULL);
            INSERT OR IGNORE INTO segment_id_sequence
            SELECT 1, max(coalesce((SELECT max(id) FROM segments), 0),
                          coalesce((SELECT max(segment_id) FROM investigations), 0));
            PRAGMA user_version=2;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def ingest(self, events, mandatory=None, codec="none", allowed_levels=None):
        normalized = normalize(events, self.policy["contracts"])
        document, blob, audit = choose(normalized, mandatory, self.policy, codec, allowed_levels)
        audit_text = encode(audit).decode()
        with self.db:
            self.db.execute("""
                UPDATE segment_id_sequence
                SET last_id = max(last_id, coalesce((SELECT max(id) FROM segments), 0)) + 1
                WHERE singleton = 1
            """)
            cur = self.db.execute("""
                INSERT INTO segments(id, document, codec, sha256, candidate_audit, input_bytes, created_at)
                SELECT last_id, ?, ?, ?, ?, ?, ? FROM segment_id_sequence WHERE singleton = 1
            """, (blob, codec, hashlib.sha256(blob).hexdigest(), audit_text,
                                   len(encode(events)), datetime.now(timezone.utc).isoformat()))
        return self.get(cur.lastrowid)

    @staticmethod
    def _validate_segment_id(sid):
        if type(sid) is not int or not 1 <= sid <= 2**63 - 1:
            raise ValueError("Use a saved group number such as 1 or 2")

    def get(self, sid):
        self._validate_segment_id(sid)
        row = self.db.execute("SELECT document,codec,sha256,candidate_audit,input_bytes,created_at FROM segments WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError("Saved group not found")
        blob, codec, digest, audit, input_bytes, created_at = row
        if hashlib.sha256(blob).hexdigest() != digest:
            raise IntegrityError("The saved information has changed since it was stored and cannot be trusted for this question")
        try:
            document = unpack(blob, codec)
            if document["schema_version"] not in READABLE_DOCUMENT_VERSIONS:
                raise IntegrityError("Unsupported evidence document version")
            decisions = json.loads(audit)
            if not isinstance(decisions, list):
                raise IntegrityError("The storage-choice notes for this group cannot be read")
        except IntegrityError:
            raise
        except (ValueError, KeyError, TypeError, zlib.error) as exc:
            raise IntegrityError("The saved information cannot be read") from exc
        return dict(document, id=sid, codec=codec, sha256=digest, candidates=decisions,
                    input_bytes=input_bytes, encoded_document_bytes=len(blob),
                    candidate_audit_bytes=len(audit.encode()), created_at=created_at)

    def list(self, limit=50, offset=0):
        self._page(limit, offset)
        result = []
        for (sid,) in self.db.execute("SELECT id FROM segments ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)):
            try:
                record = self.get(sid)
                record.pop("payload")
                result.append(record)
            except IntegrityError as exc:
                result.append({"id": sid, "integrity": "FAILED", "reason": str(exc)})
        return result

    def delete(self, sid):
        self._validate_segment_id(sid)
        with self.db:
            removed = self.db.execute("DELETE FROM segments WHERE id = ?", (sid,))
            if not removed.rowcount:
                raise KeyError("Saved group not found")
            self.db.execute("DELETE FROM investigations WHERE segment_id = ?", (sid,))

    def compression_totals(self):
        totals = {"batches": 0, "full_bytes": 0, "retained_bytes": 0}
        for audit, retained in self.db.execute("SELECT candidate_audit, length(document) FROM segments"):
            try:
                candidates = json.loads(audit)
            except (ValueError, TypeError):
                continue
            if not isinstance(candidates, list):
                continue
            full = next((item.get("encoded_bytes") for item in candidates
                         if isinstance(item, dict) and item.get("level") == "F0"), None)
            if type(full) is int and full > 0:
                totals["batches"] += 1
                totals["full_bytes"] += full
                totals["retained_bytes"] += retained
        return totals

    def stats(self):
        row = self.db.execute("SELECT count(*),coalesce(sum(input_bytes),0),coalesce(sum(length(document)),0),coalesce(sum(length(CAST(candidate_audit AS BLOB))),0) FROM segments").fetchone()
        audit = self.db.execute("SELECT count(*),coalesce(sum(length(CAST(summary AS BLOB))),0) FROM investigations").fetchone()
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        return {"segments": row[0], "input_bytes": row[1], "encoded_document_bytes": row[2],
                "candidate_audit_bytes": row[3], "queries": audit[0], "query_summary_bytes": audit[1],
                "sqlite_allocated_bytes": pages * page_size}

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..1000 and offset must be a nonnegative integer")

    def _audit(self, sid, request, result):
        summary = {"request": request, "verdict": result["verdict"], "reason": result.get("reason"),
                   "total_matches": result.get("total_matches"), "result_sha256": hashlib.sha256(encode(result)).hexdigest()}
        # Query pages are never duplicated into the audit table.
        with self.db:
            inserted = self.db.execute("""
                INSERT INTO investigations(segment_id, created_at, summary)
                SELECT id, ?, ? FROM segments WHERE id = ?
            """, (datetime.now(timezone.utc).isoformat(), encode(summary).decode(), sid))
            if not inserted.rowcount:
                raise KeyError("Saved group not found")
        return result

    def investigate(self, sid, contract, filters=None, limit=100, offset=0, search=""):
        self._page(limit, offset)
        if not isinstance(search, str) or len(search) > 256:
            raise ValueError("Answer search must be text of up to 256 characters")
        search = search.strip()
        filters = {} if filters is None else filters
        if not isinstance(filters, dict) or len(filters) > 64 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 4096 for k, v in filters.items()):
            raise ValueError("filters must map at most 64 fields to strings of at most 4096 characters")
        if not isinstance(contract, str):
            raise ValueError("contract must be a string")
        result = {"segment_id": sid, "contract": contract, "scope": SCOPE}
        request = {"mode": "records", "contract": contract, "filters": filters, "limit": limit, "offset": offset}
        if search:
            request["search"] = search
        try:
            record = self.get(sid)
        except IntegrityError as exc:
            return self._audit(sid, request, dict(result, verdict="INDETERMINATE", reason=str(exc)))
        definitions = record["contracts"]  # Historical policy snapshot is authoritative.
        if contract not in definitions:
            return self._audit(sid, request, dict(result, verdict="UNSUPPORTED", reason="This type of question is not included in the rules saved with this group"))
        if record["descriptor"][contract]["status"] != "EXACT":
            return self._audit(sid, request, dict(result, verdict="INDETERMINATE", reason="This group did not keep all details needed for this question"))
        needed = set(definitions[contract]["fields"]) | set(filters) | {"source_id", "event_id"}
        missing = sorted(f for f in needed if record["field_coverage"].get(f) != "EXACT")
        if missing:
            return self._audit(sid, request, dict(result, verdict="INDETERMINATE", missing_fields=missing, reason="Some details needed for this question were not kept for every record"))
        fields, needle = sorted(needed), search.casefold()
        page, match_count = [], 0
        for event in expand(record):
            if not all(event.get(key) == value for key, value in filters.items()):
                continue
            if needle and not any(needle in event[field].casefold() for field in fields):
                continue
            if offset <= match_count < offset + limit:
                page.append({field: event[field] for field in fields})
            match_count += 1
        result.update(verdict="SUPPORTED", answer=page, total_matches=match_count, returned=len(page),
                      offset=offset, next_offset=offset + limit if offset + limit < match_count else None,
                      reason="The needed details are available. No matches means no matches in this saved group only")
        return self._audit(sid, request, result)

    def relation(self, sid, mode, source_id, **parameters):
        """Inspect retained parent links or compare two recorded event times."""
        if mode not in ("ancestry", "order"):
            return self._audit(sid, {"mode": mode}, {"verdict": "UNSUPPORTED", "scope": SCOPE, "reason": "This question type is not available"})
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("Enter the computer/source name for this question")
        required_parameters = ("process_id",) if mode == "ancestry" else ("event_a", "event_b")
        if any(not isinstance(parameters.get(k), str) or not parameters[k] for k in required_parameters):
            raise ValueError("Required relation parameters: " + ", ".join(required_parameters))
        request = dict(parameters, mode=mode, source_id=source_id)
        result = {"segment_id": sid, "mode": mode, "scope": SCOPE}
        try:
            record = self.get(sid)
        except IntegrityError as exc:
            return self._audit(sid, request, dict(result, verdict="INDETERMINATE", reason=str(exc)))
        needed = {"source_id", "event_id", "process_id", "parent_id"} if mode == "ancestry" else {"source_id", "event_id", "timestamp"}
        if any(record["field_coverage"].get(f) != "EXACT" for f in needed):
            return self._audit(sid, request, dict(result, verdict="INDETERMINATE", reason="The saved group lacks details needed to check these program links or times"))
        events = [e for e in expand(record) if e["source_id"] == source_id]
        if mode == "order":
            by_id = {e["event_id"]: e for e in events}
            a, b = by_id.get(parameters["event_a"]), by_id.get(parameters["event_b"])
            if a is None or b is None:
                result.update(verdict="INDETERMINATE", reason="One or both record numbers were not found for this computer/source in this group")
            else:
                order = "BEFORE" if a["timestamp"] < b["timestamp"] else "AFTER" if a["timestamp"] > b["timestamp"] else "SAME_TIMESTAMP"
                result.update(verdict="SUPPORTED", relation=order, timestamps=[a["timestamp"], b["timestamp"]],
                              reason="This compares recorded times only. Equal times cannot show which activity happened first")
        else:
            parents = {}
            for event in events:
                parents.setdefault(event["process_id"], set()).add(event["parent_id"])
            current, visited, chain = parameters["process_id"], set(), []
            while True:
                if current == "ROOT":
                    result.update(verdict="SUPPORTED" if chain else "INDETERMINATE", chain=chain, reason="Followed the recorded program links to the stated starting point. Earlier computer activity may exist outside this group" if chain else "ROOT marks a starting boundary; it is not a recorded program")
                    break
                if current in visited:
                    result.update(verdict="INDETERMINATE", chain=chain, reason="The recorded program links loop back on themselves, so this history cannot be completed")
                    break
                visited.add(current)
                if current not in parents or len(parents[current]) != 1:
                    result.update(verdict="INDETERMINATE", chain=chain, reason="A program link is missing or disagrees at " + current)
                    break
                parent = next(iter(parents[current]))
                chain.append({"process_id": current, "parent_id": parent})
                current = parent
        return self._audit(sid, request, result)

    def verify_original(self, sid, events):
        try:
            record = self.get(sid)
        except IntegrityError as exc:
            return self._audit(sid, {"mode": "commitment"}, {"verdict": "INDETERMINATE", "reason": str(exc), "scope": SCOPE})
        if record["level"] != "F4":
            return self._audit(sid, {"mode": "commitment"}, {"verdict": "UNSUPPORTED", "reason": "This group keeps records rather than only a whole-group checking value", "scope": SCOPE})
        normalized = normalize(events, record["contracts"])
        match = hashlib.sha256(encode(normalized)).hexdigest() == record["payload"]["sha256"]
        return self._audit(sid, {"mode": "commitment", "provided_event_count": len(events)},
                           {"verdict": "SUPPORTED", "matches": match, "scope": SCOPE,
                            "reason": "Compared the whole supplied group with its saved checking value. This does not recover removed information or prove the source is true"})

    def backup(self, filename):
        destination = Path(filename)
        if destination.exists():
            raise ValueError("Backup destination already exists")
        with sqlite3.connect(str(destination)) as target:
            self.db.backup(target)
