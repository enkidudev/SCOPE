import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Handler, HTTPServer
from demo import fixture


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.server.dbfile = str(Path(cls.tmp.name) / "test.db")
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()

    def post(self, endpoint, data, headers=None):
        request = urllib.request.Request(self.url + endpoint, data=json.dumps(data).encode(),
                                          headers={"Content-Type": "application/json", **(headers or {})})
        return json.load(urllib.request.urlopen(request))

    def test_health_page_and_state(self):
        self.assertEqual(json.load(urllib.request.urlopen(self.url + "/api/health"))["status"], "ok")
        self.assertIn(b"Ask questions", urllib.request.urlopen(self.url).read())
        self.assertIn("stats", json.load(urllib.request.urlopen(self.url + "/api/state")))

    def test_guide_is_available_locally(self):
        response = urllib.request.urlopen(self.url + "/guide")
        self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))
        self.assertIn(b"SCOPE Complete Project Guide", response.read())

    def test_dashboard_script_is_served(self):
        response = urllib.request.urlopen(self.url + "/dashboard.js")
        self.assertTrue(response.headers["Content-Type"].startswith("application/javascript"))
        self.assertIn(b"function readFilters", response.read())

    def test_demo_query_pagination_export(self):
        record = self.post("/api/demo", {"count": 12})
        answer = self.post("/api/query", {"segment_id": record["id"], "contract": "EC-01", "limit": 5})
        self.assertEqual((answer["total_matches"], answer["returned"]), (12, 5))
        exported = json.load(urllib.request.urlopen(self.url + "/api/export/" + str(record["id"])))
        self.assertEqual(exported["id"], record["id"])

    def test_search_and_deletion_update_saved_compression(self):
        def state():
            return json.load(urllib.request.urlopen(self.url + "/api/state?limit=1"))

        before = state()
        events = fixture(150)
        events[-1]["principal"] = "Unique Analyst"
        first = self.post("/api/ingest", {"events": events, "codec": "zlib"})
        second = self.post("/api/demo", {"count": 12})
        query = {"segment_id": first["id"], "contract": "EC-01", "limit": 1}
        self.assertNotEqual(self.post("/api/query", query)["answer"][0]["principal"], "Unique Analyst")
        result = self.post("/api/query", {**query, "search": "UNIQUE analyst"})
        self.assertEqual((result["total_matches"], result["answer"][0]["principal"]), (1, "Unique Analyst"))
        self.assertEqual(self.post("/api/query", {**query, "search": "missing value"})["total_matches"], 0)

        totals = state()["compression"]
        self.assertEqual(totals["batches"], before["compression"]["batches"] + 2)
        self.assertEqual(totals["retained_bytes"], before["compression"]["retained_bytes"]
                         + first["encoded_document_bytes"] + second["encoded_document_bytes"])
        baseline = next(item["encoded_bytes"] for item in first["candidates"] if item["level"] == "F0")
        self.assertEqual(self.post("/api/delete", {"segment_id": first["id"]}), {"deleted": first["id"]})
        after = state()
        self.assertEqual(after["stats"]["queries"], before["stats"]["queries"])
        self.assertEqual(after["compression"]["full_bytes"], totals["full_bytes"] - baseline)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + "/api/export/" + str(first["id"]))
        self.assertEqual(caught.exception.code, 404)
        self.post("/api/delete", {"segment_id": second["id"]})
        replacement = self.post("/api/demo", {"count": 12})
        self.assertGreater(replacement["id"], second["id"])

    def test_invalid_json_types_and_event_fields(self):
        for endpoint, data in (("/api/demo", []), ("/api/ingest", {"events": []}), ("/api/query", {})):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post(endpoint, data)
            self.assertEqual(caught.exception.code, 400)

    def test_origin_and_host_validation(self):
        for headers in ({"Origin": "https://example.org"}, {"Host": "attacker.example"}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/api/demo", {}, headers)
            self.assertEqual(caught.exception.code, 403)

    def test_wrong_content_type(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/demo", {}, {"Content-Type": "text/plain"})
        self.assertEqual(caught.exception.code, 415)

    def test_relation_endpoint(self):
        record = self.post("/api/demo", {"scenario": "chain"})
        answer = self.post("/api/query", {"segment_id": record["id"], "mode": "ancestry", "source_id": "lab-win01", "process_id": "proc-3"})
        self.assertEqual(answer["verdict"], "SUPPORTED")

    def test_transport_limit(self):
        request = urllib.request.Request(self.url + "/api/demo", data=b"{}", headers={"Content-Type": "application/json", "Content-Length": "20000001"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 413)


class CLITests(unittest.TestCase):
    def test_documented_cli_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            def cli(*args):
                result = subprocess.run([sys.executable, str(ROOT / "app.py"), "--db", str(Path(directory) / "cli.db"), *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            record = cli("demo", "--scenario", "chain", "--codec", "zlib")
            sid = str(record["id"])
            self.assertEqual(cli("query", sid, "EC-01")["total_matches"], 4)
            self.assertEqual(cli("ancestry", sid, "lab-win01", "proc-3")["verdict"], "SUPPORTED")
            self.assertEqual(cli("order", sid, "lab-win01", "0", "1")["relation"], "BEFORE")
            cli("export", sid, str(Path(directory) / "export.json"))
            cli("backup", str(Path(directory) / "backup.db"))
            self.assertEqual(cli("stats")["segments"], 1)
            self.assertEqual(len(cli("list")), 1)

    def test_plain_command_names_keep_the_original_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "plain.db")
            def cli(*args):
                result = subprocess.run([sys.executable, str(ROOT / "app.py"), "--db", database, *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            record = cli("add-example", "--scenario", "chain")
            sid = str(record["id"])
            self.assertEqual(cli("ask", sid, "EC-01")["total_matches"], 4)
            self.assertEqual(cli("program-history", sid, "lab-win01", "proc-3")["verdict"], "SUPPORTED")
            self.assertEqual(cli("compare-times", sid, "lab-win01", "0", "1")["relation"], "BEFORE")
            self.assertEqual(len(cli("saved")), 1)
            self.assertEqual(cli("storage")["segments"], 1)
            cli("add-file", str(ROOT / "examples/demo-events.json"))
            cli("add-windows", str(ROOT / "examples/sysmon-network-synthetic.xml"), "--contracts", "EC-04,EC-05")
            cli("add-file", str(ROOT / "examples/demo-events.json"), "--contracts", "none", "--levels", "F4")
            self.assertTrue(cli("check-original", "4", str(ROOT / "examples/demo-events.json"))["matches"])

    def test_cli_error_is_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "app.py"), "--db", str(Path(directory) / "cli.db"), "demo", "--contracts", "INVALID"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Unknown contracts", result.stderr)
