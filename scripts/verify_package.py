"""Check included files against SHA256SUMS.txt; not an authenticity signature."""
import hashlib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
failures = []
for line in (root / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
    digest, name = line.split('  ', 1)
    target = root / name
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        failures.append(name)
if failures:
    raise SystemExit('Verification failed: ' + ', '.join(failures))
print('All packaged files match the manifest. This checks integrity, not publisher authenticity.')
