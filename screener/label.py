#!/usr/bin/env python3
"""Build ground-truth labels with as little hand-labeling as possible.

Where the regex baseline and every pipeline agree, the label is taken as
given ("consensus"). Every disagreement is shown to a human. A random
spot-check of consensus cases measures how often that shortcut is wrong.

    python label.py                 # label all disagreements
    python label.py --spot-check 10 # also hand-check 10 consensus cases
    python label.py --split test --all   # held-out set: label every job by hand,
                                         # blind (system predictions hidden)

Labels are saved to labels.json after every answer, so you can quit anytime.
"""

import argparse
import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = {"e": "eligible", "n": "no_sponsorship", "c": "citizen_only"}
# A JD with none of these words can't carry a sponsorship/citizenship
# restriction. Checked on the hand-labeled dev set: 20/20 such JDs eligible.
ANY_RESTRICTION_WORD = re.compile(
    r"sponsor|citizen|export|clearance|permanent resident|authoriz|visa|ITAR|U\.?S\.? person", re.I)


def labels_path(split="dev"):
    return ROOT / ("labels_test.json" if split == "test" else "labels.json")


def predictions(split="dev"):
    from run import load_jobs, results_dir
    jobs = {j["id"]: j for j in load_jobs(split)}
    preds = {jid: {"regex": (j["regex_verdict"], j.get("regex_evidence", ""))} for jid, j in jobs.items()}
    for f in sorted(results_dir(split).glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if "error" in r:
                continue
            e = r["extraction"]
            quote = " | ".join(q for q in (e["sponsorship_quote"], e["citizenship_quote"]) if q)
            preds[r["id"]][f.stem] = (r["verdict"], quote)
    return jobs, preds


def context(text, quote, width=300):
    if not quote:
        return ""
    probe = quote.split(" | ")[0][:60]
    i = text.lower().find(probe.lower())
    return "…" + text[max(0, i - width):i + len(probe) + width] + "…" if i >= 0 else ""


def ask(job, preds, blind=False):
    print("\n" + "=" * 78)
    print(f"{job['company']} — {job['title'] or job['role']}\n{job['url']}")
    for name, (v, ev) in ({} if blind else preds).items():
        print(f"\n  [{name}] {v}")
        if ev:
            print("    evidence:", re.sub(r"\s+", " ", ev)[:300])
    for kw in ("sponsor", "citizen", "export", "clearance", "permanent resident", "authoriz"):
        for m in list(re.finditer(kw, job["text"], re.I))[:2]:
            print(f"\n  ...{job['text'][max(0, m.start() - 200):m.end() + 200]}...")
    while True:
        a = input("\n  label? [e]ligible / [n]o_sponsorship / [c]itizen_only / [s]kip / [q]uit: ").strip().lower()
        if a in KEYS or a in ("s", "q"):
            return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot-check", type=int, default=0)
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    ap.add_argument("--all", action="store_true", help="hand-label every job, no consensus shortcut")
    args = ap.parse_args()

    LABELS = labels_path(args.split)
    jobs, preds = predictions(args.split)
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    todo = []
    for jid, p in preds.items():
        verdicts = {v for v, _ in p.values()}
        if args.all and not ANY_RESTRICTION_WORD.search(jobs[jid]["text"]):
            labels.setdefault(jid, {"label": "eligible", "source": "auto_no_restriction_words"})
        elif not args.all and len(verdicts) == 1 and len(p) > 1:
            labels.setdefault(jid, {"label": verdicts.pop(), "source": "consensus"})
        elif labels.get(jid, {}).get("source") != "human":
            todo.append(jid)
    consensus = [j for j, l in labels.items() if l["source"] == "consensus"]
    todo += random.Random(0).sample(consensus, min(args.spot_check, len(consensus)))
    auto = sum(l["source"] == "auto_no_restriction_words" for l in labels.values())
    print(f"{len(preds)} jobs, {len(consensus)} consensus, {auto} auto-eligible, {len(todo)} to label by hand")

    for jid in todo:
        a = ask(jobs[jid], preds[jid], blind=args.all)
        if a == "q":
            break
        if a == "s":
            continue
        prev = labels.get(jid, {})
        labels[jid] = {"label": KEYS[a], "source": "human",
                       "spot_check": prev.get("source") == "consensus",
                       "consensus_was": prev.get("label") if prev.get("source") == "consensus" else None}
        LABELS.write_text(json.dumps(labels, indent=1))
    LABELS.write_text(json.dumps(labels, indent=1))
    print(f"saved {len(labels)} labels")


if __name__ == "__main__":
    main()
