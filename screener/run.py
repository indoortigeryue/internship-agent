#!/usr/bin/env python3
"""Run one pipeline config over the corpus.

    python run.py --config single          # one Claude call per JD
    python run.py --config multi_same      # Claude extractor + Claude verifier
    python run.py --config multi_cross     # Claude extractor + GPT verifier
    python run.py --config cascade         # Haiku triage, Opus only when uncertain
    python run.py --config multi_cross --limit 5   # smoke test

Results:  results/<config>.jsonl  (one line per JD, with every agent step)
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import agents

ROOT = Path(__file__).resolve().parent
CLAUDE = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
GPT = "gpt-5.5"

CONFIGS = {
    "single": dict(extractor_model=CLAUDE, verifier_model=None, grounding=False),
    "single_gpt": dict(extractor_model=GPT, verifier_model=None, grounding=False),
    "single_haiku": dict(extractor_model=HAIKU, verifier_model=None, grounding=False),
    "grounded": dict(extractor_model=CLAUDE, verifier_model=None, grounding=True),
    "multi_same": dict(extractor_model=CLAUDE, verifier_model=CLAUDE),
    "multi_cross": dict(extractor_model=CLAUDE, verifier_model=GPT),
    "cascade": dict(triage_model=HAIKU, specialist_model=CLAUDE),
}


def load_jobs(split="dev"):
    """Full JD records from data/jds/ (local only: scraped text isn't published).
    Without them, fall back to corpus.json, the committed manifest of the same
    jobs minus the text: enough to re-score results, not to re-run models."""
    paths = sorted((ROOT / "data" / "jds").glob("*.json"))
    if paths:
        jobs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    else:
        jobs = json.loads((ROOT / "corpus.json").read_text(encoding="utf-8"))
    return [j for j in jobs if j.get("split", "dev") == split]


def results_dir(split="dev"):
    return ROOT / "results" / ("test" if split == "test" else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=CONFIGS, required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    args = ap.parse_args()

    jobs = load_jobs(args.split)[:args.limit]
    cfg = CONFIGS[args.config]

    def one(job):
        try:
            if args.config == "cascade":
                return agents.cascade(job, **cfg)
            return agents.screen(job, **cfg)
        except Exception as e:  # keep going; failures are reported, not hidden
            return {"id": job["id"], "error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(one, jobs))

    out = results_dir(args.split) / f"{args.config}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8")

    ok = [r for r in results if "error" not in r]
    errs = [r for r in results if "error" in r]
    fresh = sum(s.get("cost", 0) for r in ok for s in r["steps"] if not s.get("cache_hit"))
    print(f"{args.config}: {len(ok)}/{len(results)} ok, "
          f"avg rounds {sum(r['rounds'] for r in ok) / max(len(ok), 1):.2f}, "
          f"spent ${fresh:.3f} this run (rest from cache)"
          + (f", escalated {sum(r['escalated'] for r in ok)}/{len(ok)}" if args.config == "cascade" else ""))
    for r in errs[:5]:
        print("  error", r["id"], r["error"][:200])


if __name__ == "__main__":
    main()
