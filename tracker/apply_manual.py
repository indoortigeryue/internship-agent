#!/usr/bin/env python3
"""Overlay the manually verified verdicts in manual_verdicts.json onto data/verified.json.

Some sites block scrapers (Tesla returns 403) or render entirely in JS (some ATSes), so
`verify.py` can't fetch content and can only say blocked / unknown. After such jobs are
checked by hand in a browser, the verdicts go in manual_verdicts.json and this script
reapplies them after every verify run -- otherwise `--recheck` would wipe them out.

Runs after verify.py and before dashboard.py.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "verified.json"
MANUAL = ROOT / "manual_verdicts.json"

# Your own verdicts (gitignored); manual_verdicts.example.json documents the format.
if not MANUAL.exists():
    print("no manual_verdicts.json, nothing to apply")
    raise SystemExit(0)
cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
manual = {k: v for k, v in json.loads(MANUAL.read_text(encoding="utf-8")).items()
          if not k.startswith("_")}

applied = 0
for url, v in manual.items():
    prev = cache.get(url, {})
    cache[url] = {**prev, **v, "manual": True}
    applied += 1
    print(f"  ✓ {v.get('real_title') or url[:60]}  [{v['verdict']}]")

CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\nApplied {applied} manual verification verdicts")
