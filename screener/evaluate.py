#!/usr/bin/env python3
"""Score every system against labels.json and print the comparison table.

Primary metric is the decision that matters to the candidate — apply or
skip:
  false rejection = an eligible job screened out (a missed opportunity)
  false accept    = a restricted job let through (a wasted application)
"""

import json
from pathlib import Path

from label import labels_path, predictions
from run import results_dir

ROOT = Path(__file__).resolve().parent
ORDER = ["regex", "single", "single_haiku", "single_gpt", "grounded", "multi_same", "multi_cross", "cascade"]


def costs(split):
    out = {}
    for f in results_dir(split).glob("*.jsonl"):
        rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
        ok = [r for r in rows if "error" not in r]
        out[f.stem] = {
            "cost": sum(r["cost"] for r in ok) / max(len(ok), 1),
            "latency": sum(r["latency_s"] for r in ok) / max(len(ok), 1),
            "rounds": sum(r["rounds"] for r in ok) / max(len(ok), 1),
            "errors": len(rows) - len(ok),
        }
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    split = ap.parse_args().split
    _, preds = predictions(split)
    labels = json.loads(labels_path(split).read_text())
    c = costs(split)
    spot = [l for l in labels.values() if l.get("spot_check")]
    wrong = sum(l["label"] != l["consensus_was"] for l in spot)
    print(f"{len(labels)} labeled jobs "
          f"({sum(l['source'] == 'human' for l in labels.values())} by hand"
          + (f"; consensus spot-check: {len(spot) - wrong}/{len(spot)} correct" if spot else "") + ")\n")

    print("| System | Accuracy (3-class) | Apply/skip accuracy | False rejections | False accepts | $/job | Latency/job | Avg rounds |")
    print("|---|---|---|---|---|---|---|---|")
    systems = sorted({s for p in preds.values() for s in p}, key=lambda s: ORDER.index(s) if s in ORDER else 99)
    for s in systems:
        rows = [(p[s][0], labels[j]["label"]) for j, p in preds.items() if s in p and j in labels]
        n = len(rows)
        if not n:
            continue
        acc3 = sum(a == b for a, b in rows) / n
        acc2 = sum((a == "eligible") == (b == "eligible") for a, b in rows) / n
        elig = [(a, b) for a, b in rows if b == "eligible"]
        restr = [(a, b) for a, b in rows if b != "eligible"]
        fr = sum(a != "eligible" for a, _ in elig)
        fa = sum(a == "eligible" for a, _ in restr)
        m = c.get(s, {"cost": 0, "latency": 0, "rounds": 0})
        print(f"| {s} | {acc3:.0%} | {acc2:.0%} | {fr}/{len(elig)} | {fa}/{len(restr)} | "
              f"${m['cost']:.4f} | {m['latency']:.1f}s | {m['rounds']:.2f} |")


if __name__ == "__main__":
    main()
