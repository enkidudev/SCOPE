# SCOPE

**Security-Contracted Observability and Preservation**

SCOPE is a local workspace for importing security records, selecting the details an investigation needs, and measuring how much storage those choices save.

## Start

1. Extract `SCOPE.zip` and open the `scope` folder.
2. On Windows, double-click `START_WINDOWS.bat`. On Parrot/Linux, run `sh start.sh`.
3. Open <http://127.0.0.1:8765>. Click **Open SCOPE**, then **Try a guided example** in Overview or import your own records under **Add records**.

Python 3.10 or later is required. Keep the terminal open while working, and press Ctrl+C to stop the server. For a separate database or a different port, run `python app.py --db practice.db start --port 8877`. Use `py -3` on Windows or `python3` on Linux if needed.

## Read Results

The homepage compares full and retained document sizes across saved batches that have a measured baseline. Each batch uses the same codec for both sides of its comparison. The totals update when you add or delete a batch.

**Live results** uses the engine's recorded byte counts for the latest 50 batches. Each comparison uses the same codec for the full and retained documents. SQLite allocation includes indexes and notes and is shown separately. **Benchmarks** uses packaged measurements from synthetic workloads and reports sizes, evidence checks, and median timings.

## Work with saved records

Use **Search answer records** to look across all pages of a record answer by a displayed field value. In **Saved records**, **Delete** removes the chosen batch and its investigation history after confirmation.

## Stack

| Component | Role and reason |
| --- | --- |
| Python and its standard library | Runs the retention engine, local HTTP and JSON API, Sysmon XML importer, CLI, and tests. The core runs with one Python installation. |
| SQLite | Persists documents, retention decisions, and query summaries in one local file. It supplies transactions and backups without operating a database server. |
| JSON and SHA-256 | Canonical JSON gives the engine repeatable byte counts and portable exports. The stored SHA-256 digest detects changes to the retained document bytes. |
| zlib | Built-in, lossless compression. It packs the encoded document after retention rules have been evaluated. |
| Zstandard | Optional codec for a speed and size comparison. Install `requirements-benchmark.txt` when writing or reading documents that use it, or when running its benchmark cases. |
| HTML, CSS, and JavaScript | Provide the local dashboard, glass styling, and chart controls. Chart widths use measured bytes and the browser renders them without any third-party assets. |
| PowerShell | Optional Windows helper for exporting supported Sysmon XML. SCOPE imports supplied files and runs without a collector. |
| Node.js | Optional runtime for the dashboard's DOM simulation tests. |

[Complete project guide](docs/SCOPE_Project_Guide.md)

## Check and reproduce

```sh
python scripts/verify_package.py
python -m unittest discover -s tests -v
node tests/test_dashboard.js
python benchmark.py --events 300 --trials 3
```

Read `python benchmark.py --help` to specify output paths. The packaged benchmark uses synthetic events. Savings and timings will vary with the workload, selected rules, codec, and machine.
