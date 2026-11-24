"""SCOPE local dashboard, JSON API and command-line entry point."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from demo import SCENARIOS, fixture
from engine import VERSION, IntegrityError, Store, encode, load_policy
from import_sysmon import convert

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data, content_type="application/json", download=None):
        body = data.encode("utf-8") if isinstance(data, str) else encode(data)
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="' + download + '"')
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        hosts = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
        if self.headers.get("Host") not in hosts:
            self.send(403, {"error": "Open SCOPE at its address on this computer, such as http://127.0.0.1:8765"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + h for h in hosts}:
            self.send(403, {"error": "Open SCOPE directly on this computer before making this request"})
            return False
        return True

    def store(self):
        return Store(self.server.dbfile, getattr(self.server, "policy_file", None))

    def do_GET(self):
        if not self.local_request():
            return
        try:
            target = urlparse(self.path)
            query = parse_qs(target.query)
            if target.path == "/":
                return self.send(200, (ROOT / "dashboard.html").read_text(encoding="utf-8"), "text/html")
            assets = {"/dashboard.js": "application/javascript", "/dashboard.css": "text/css"}
            if target.path in assets:
                return self.send(200, (ROOT / target.path[1:]).read_text(encoding="utf-8"), assets[target.path])
            if target.path == "/api/health":
                return self.send(200, {"status": "ok", "version": VERSION})
            if target.path == "/guide":
                return self.send(200, (ROOT / "docs" / "SCOPE_Project_Guide.md").read_text(encoding="utf-8"), "text/plain")
            if target.path == "/api/benchmark":
                file = ROOT / "data" / "benchmark.json"
                if not file.exists():
                    return self.send(404, {"error": "No stored comparison results were found. Run python benchmark.py to create them"})
                return self.send(200, json.loads(file.read_text(encoding="utf-8")))
            with self.store() as store:
                if target.path == "/api/state":
                    limit, offset = int(query.get("limit", [50])[0]), int(query.get("offset", [0])[0])
                    store.db.execute("BEGIN")
                    return self.send(200, {"version": VERSION, "policy": store.policy, "stats": store.stats(),
                                           "compression": store.compression_totals(), "segments": store.list(limit, offset),
                                           "limit": limit, "offset": offset})
                for prefix in ("/api/segment/", "/api/export/"):
                    if target.path.startswith(prefix):
                        sid = int(target.path[len(prefix):])
                        return self.send(200, store.get(sid), download=f"scope-segment-{sid}.json" if prefix == "/api/export/" else None)
            self.send(404, {"error": "Not found"})
        except KeyError as exc:
            self.send(404, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self.send(400, {"error": str(exc)})
        except (sqlite3.Error, OSError):
            self.send(500, {"error": "SCOPE could not read or save a local file. Check the database location and file permissions"})

    def do_POST(self):
        if not self.local_request():
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send(415, {"error": "Content-Type application/json is required"})
            if self.headers.get("Transfer-Encoding"):
                return self.send(400, {"error": "Chunked request bodies are not supported"})
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 20_000_000:
                return self.send(413, {"error": "HTTP request must contain 1 to 20000000 bytes"})
            self.connection.settimeout(15)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            with self.store() as store:
                if self.path in ("/api/demo", "/api/ingest", "/api/import-sysmon"):
                    if self.path == "/api/demo":
                        events = fixture(data.get("count", 600), data.get("scenario", "repetitive"))
                    elif self.path == "/api/import-sysmon":
                        events = convert(data["xml"])
                    else:
                        events = data["events"]
                    result = store.ingest(events, data.get("mandatory"), data.get("codec", "none"), data.get("allowed_levels"))
                elif self.path == "/api/query":
                    mode = data.get("mode", "records")
                    sid = data["segment_id"]
                    if mode == "records":
                        result = store.investigate(sid, data["contract"], data.get("filters"), data.get("limit", 100), data.get("offset", 0), data.get("search", ""))
                    else:
                        parameters = {k: data[k] for k in ("process_id", "event_a", "event_b") if k in data}
                        result = store.relation(sid, mode, data.get("source_id"), **parameters)
                elif self.path == "/api/delete":
                    sid = data["segment_id"]
                    try:
                        store.delete(sid)
                    except KeyError:
                        return self.send(404, {"error": "Saved group not found"})
                    result = {"deleted": sid}
                elif self.path == "/api/verify-original":
                    result = store.verify_original(data["segment_id"], data["events"])
                else:
                    return self.send(404, {"error": "Not found"})
                return self.send(200, result)
        except KeyError as exc:
            self.send(400, {"error": "Missing field or segment: " + str(exc)})
        except (ValueError, TypeError, UnicodeError) as exc:
            self.send(400, {"error": str(exc)})
        except (sqlite3.Error, OSError):
            self.send(500, {"error": "SCOPE could not read or save a local file. Check the database location and file permissions"})


def read_json(filename):
    return json.loads(Path(filename).read_text(encoding="utf-8-sig"))


def parse_contracts(value):
    if value is None:
        return None
    if value.lower() == "none":
        return []
    result = [v.strip() for v in value.split(",")]
    if not all(result):
        raise ValueError("Use comma-separated IDs, or the explicit word none")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="scope_v02.db", help="Database file to open or create")
    parser.add_argument("--policy", help="Optional rule file; each saved group keeps its original rules")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", aliases=["start"], help="Open SCOPE on this computer")
    serve.add_argument("--port", type=int, default=8765)
    demo = sub.add_parser("demo", aliases=["add-example"], help="Add made-up example records")
    demo.add_argument("--scenario", choices=SCENARIOS, default="repetitive")
    demo.add_argument("--count", type=int, default=600)
    ingest = sub.add_parser("ingest", aliases=["add-file"], help="Add records from a SCOPE JSON file")
    ingest.add_argument("file")
    xml = sub.add_parser("import-sysmon", aliases=["add-windows"], help="Add supported Windows program/connection XML records")
    xml.add_argument("file")
    for command in (demo, ingest, xml):
        command.add_argument("--contracts", help="Required rule IDs separated by commas; default all. Use none to allow all record content to be removed")
        command.add_argument("--codec", choices=("none", "zlib", "zstd"), default="none")
        command.add_argument("--levels", help="Optional allowed storage choices, e.g. F1,F2; see the guide")
    query = sub.add_parser("query", aliases=["ask"], help="Find recorded details in one saved group")
    query.add_argument("segment_id", type=int)
    query.add_argument("contract")
    filters = query.add_mutually_exclusive_group()
    filters.add_argument("--filters", default="{}")
    filters.add_argument("--filters-file")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--offset", type=int, default=0)
    ancestry = sub.add_parser("ancestry", aliases=["program-history"], help="Follow recorded links to programs that started other programs")
    ancestry.add_argument("segment_id", type=int); ancestry.add_argument("source_id"); ancestry.add_argument("process_id")
    order = sub.add_parser("order", aliases=["compare-times"], help="Compare times for two records from the same computer/source")
    order.add_argument("segment_id", type=int); order.add_argument("source_id"); order.add_argument("event_a"); order.add_argument("event_b")
    verify = sub.add_parser("verify-original", aliases=["check-original"], help="Compare a whole supplied group with a saved F4 checking value")
    verify.add_argument("segment_id", type=int); verify.add_argument("file")
    export = sub.add_parser("export", help="Save a readable copy of retained information and storage notes")
    export.add_argument("segment_id", type=int); export.add_argument("file")
    backup = sub.add_parser("backup", help="Create a separate database backup; existing files are kept")
    backup.add_argument("file")
    listing = sub.add_parser("list", aliases=["saved"], help="List saved groups")
    listing.add_argument("--limit", type=int, default=50); listing.add_argument("--offset", type=int, default=0)
    sub.add_parser("stats", aliases=["storage"], help="Show separate record, note and database sizes")
    args = parser.parse_args(argv)
    aliases = {"start": "serve", "add-example": "demo", "add-file": "ingest", "add-windows": "import-sysmon",
               "ask": "query", "program-history": "ancestry", "compare-times": "order", "check-original": "verify-original",
               "saved": "list", "storage": "stats"}
    args.command = aliases.get(args.command, args.command)
    try:
        if args.command == "serve":
            with Store(args.db, args.policy):
                pass
            server = HTTPServer(("127.0.0.1", args.port), Handler)
            server.dbfile, server.policy_file = args.db, args.policy
            print(f"SCOPE running at http://127.0.0.1:{args.port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        with Store(args.db, args.policy) as store:
            if args.command in ("demo", "ingest", "import-sysmon"):
                if args.command == "demo":
                    events = fixture(args.count, args.scenario)
                elif args.command == "ingest":
                    events = read_json(args.file)
                else:
                    events = convert(Path(args.file).read_text(encoding="utf-8-sig"))
                result = store.ingest(events, parse_contracts(args.contracts), args.codec,
                                      args.levels.split(",") if args.levels else None)
            elif args.command == "query":
                filters = read_json(args.filters_file) if args.filters_file else json.loads(args.filters)
                result = store.investigate(args.segment_id, args.contract, filters, args.limit, args.offset)
            elif args.command == "ancestry":
                result = store.relation(args.segment_id, "ancestry", args.source_id, process_id=args.process_id)
            elif args.command == "order":
                result = store.relation(args.segment_id, "order", args.source_id, event_a=args.event_a, event_b=args.event_b)
            elif args.command == "verify-original":
                result = store.verify_original(args.segment_id, read_json(args.file))
            elif args.command == "export":
                destination = Path(args.file)
                if destination.exists():
                    raise ValueError("Export destination already exists")
                destination.write_text(json.dumps(store.get(args.segment_id), indent=2), encoding="utf-8")
                result = {"exported": str(destination)}
            elif args.command == "backup":
                store.backup(args.file); result = {"backup": args.file}
            elif args.command == "list":
                result = store.list(args.limit, args.offset)
            else:
                result = store.stats()
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print("SCOPE: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
