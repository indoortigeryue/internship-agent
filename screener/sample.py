#!/usr/bin/env python3
"""Build the eval corpus: a stratified sample of real internship postings,
with full JD text and the regex baseline's verdict.

Source: ../tracker/data/verified.json — 600 postings the rule-based
screener (tracker/verify.py) has already judged. We reuse its ATS fetchers
(Workday / Greenhouse / Oracle / Ashby APIs) to pull the JD text again, since
the tracker only cached verdicts, not text.

Usage:
    python sample.py            # sample + fetch, writes data/jds/*.json
    python sample.py --dry-run  # just print what would be sampled
    python sample.py --split test --seed 11 --per-class 25,10,10
                                # held-out set: only postings not already sampled
"""

import argparse
import hashlib
import json
import random
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRACKER = ROOT.parent / "tracker"
sys.path.insert(0, str(TRACKER))
import verify  # noqa: E402  (the tracker's rule-based screener + ATS fetchers)
from agents import dedupe  # noqa: E402

OUT = ROOT / "data" / "jds"

# Collapse the tracker's verdicts onto the one question the agents answer:
# can an F-1 student on CPT take this internship?
LABEL = {"ok": "eligible", "cpp_only": "eligible", "title_mismatch": "eligible",
         "no_sponsorship": "no_sponsorship", "citizen_only": "citizen_only"}

# Postings the tracker's code comments call out as regex traps. Always included.
HARD_CASES = ["Datadog", "Mary Kay", "Michelin", "Snap", "Cloudflare", "Walmart"]

PER_CLASS = {"eligible": 40, "no_sponsorship": 25, "citizen_only": 25}
MAX_CHARS = 60_000  # ~15k tokens; longer pages are flagged, not silently cut


def job_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def pick(cache, seed, per_class=PER_CLASS, exclude=(), hard_cases=True):
    rows = [dict(url=u, **v) for u, v in cache.items()
            if v["verdict"] in LABEL and job_id(u) not in exclude]
    rng = random.Random(seed)
    rng.shuffle(rows)
    chosen = {r["url"]: r for r in rows if hard_cases
              and any(h.lower() in (r.get("company") or "").lower() for h in HARD_CASES)}
    for cls, n in per_class.items():
        pool = [r for r in rows if LABEL[r["verdict"]] == cls and r["url"] not in chosen]
        have = sum(LABEL[r["verdict"]] == cls for r in chosen.values())
        for r in pool[:max(0, n - have)]:
            chosen[r["url"]] = r
    return list(chosen.values())


def fetch_one(r, split="dev"):
    path = OUT / f"{job_id(r['url'])}.json"
    if path.exists():
        return "cached"
    # LinkedIn (80% of the tracker's postings) answers 429 under any real load
    for wait in (0, 15, 45, 90):
        time.sleep(wait)
        try:
            status, raw, title, _ = verify.fetch_jd(r["url"], r.get("company", ""))
        except Exception as e:  # network code from another project; don't let one job kill the run
            return f"error: {e}"
        if status != 429:
            break
    if raw.startswith("__ERROR__") or not status or status >= 400:
        return f"fetch failed ({status})"
    text = dedupe(verify.page_text(raw))
    if len(text) < 400:
        return "too short (JS-rendered?)"
    rec = {
        "id": job_id(r["url"]), "split": split, "url": r["url"],
        "company": r.get("company", ""), "role": r.get("role", ""),
        "title": title or r.get("real_title", ""),
        "regex_verdict": LABEL[r["verdict"]], "regex_evidence": r.get("note", ""),
        "truncated": len(text) > MAX_CHARS, "text": text[:MAX_CHARS],
    }
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    ap.add_argument("--per-class", help="eligible,no_sponsorship,citizen_only counts")
    args = ap.parse_args()

    cache = json.loads((TRACKER / "data" / "verified.json").read_text(encoding="utf-8"))
    per_class = PER_CLASS
    if args.per_class:
        per_class = dict(zip(PER_CLASS, map(int, args.per_class.split(","))))
    # never resample anything already in the corpus, including fetch failures
    exclude = {p.stem for p in OUT.glob("*.json")}
    if args.split == "test":
        exclude |= {job_id(r["url"]) for r in pick(cache, 7)}
    rows = pick(cache, args.seed, per_class, exclude if args.split == "test" else (),
                hard_cases=args.split == "dev")
    by = {}
    for r in rows:
        by[LABEL[r["verdict"]]] = by.get(LABEL[r["verdict"]], 0) + 1
    print(f"sampled {len(rows)}: {by}")
    if args.dry_run:
        return

    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(lambda r: fetch_one(r, args.split), rows))
    for r, res in zip(rows, results):
        if res not in ("ok", "cached"):
            print(f"  skip {r.get('company')}: {res}")
    print(f"done: {sum(x in ('ok', 'cached') for x in results)}/{len(rows)} with JD text")


if __name__ == "__main__":
    main()
