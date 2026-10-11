#!/usr/bin/env python3
"""Manually remove jobs: ones not worth applying to, or added by mistake, are permanently
dropped from the to-apply list.

Why not just mark them skipped in seen.json: the tracker regenerates from the sources
daily, and if upstream edits the title the key changes, so the job reappears as new the
next day. So the blacklist is keyed on **company + title with term/year stripped**, with
the URL recorded as a second safeguard.

Usage:
    python3 remove_job.py "Walmart"                  # match by keyword (asks for confirmation)
    python3 remove_job.py "Walmart" --yes            # skip confirmation
    python3 remove_job.py <url>                      # remove by link
    python3 remove_job.py "Oshkosh" --reason "wrong track"
    python3 remove_job.py --list                     # show the blacklist
    python3 remove_job.py --undo "Walmart"           # release from the blacklist
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXCLUDED = ROOT / "manual_excluded.json"
MANUAL_JOBS = ROOT / "manual_jobs.json"
JOBS = ROOT / "data" / "jobs.json"
SEEN = ROOT / "data" / "seen.json"
sys.path.insert(0, str(ROOT))
from tracker import role_base  # noqa: E402  same title normalization used for dedup


def norm_company(c):
    return re.sub(r"[^a-z0-9]+", "", c.lower())


def load_excluded():
    if EXCLUDED.exists():
        return json.loads(EXCLUDED.read_text(encoding="utf-8"))
    return {"_comment": "Blocklist of manually removed jobs. tracker.py checks it on every filter pass, "
                        "taking priority over all other rules (including manually added jobs). Maintained by remove_job.py.",
            "excluded": []}


def save_excluded(d):
    EXCLUDED.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", help="Company/title keyword, or the full job URL")
    ap.add_argument("--reason", default="Not interested")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--undo", metavar="KEYWORD", help="Remove from the blocklist")
    a = ap.parse_args()

    data = load_excluded()

    if a.list:
        if not data["excluded"]:
            print("The blocklist is empty")
            return
        print(f"Blocklist ({len(data['excluded'])}):\n")
        for e in data["excluded"]:
            print(f"  · {e['company']} — {e['role']}")
            print(f"    Reason: {e['reason']} · removed on {e['removed']}")
        return

    if a.undo:
        kw = a.undo.lower()
        keep, gone = [], []
        for e in data["excluded"]:
            (gone if kw in f"{e['company']} {e['role']}".lower() else keep).append(e)
        if not gone:
            print(f"Nothing in the blocklist matches '{a.undo}'")
            return
        for e in gone:
            print(f"  Restored: {e['company']} — {e['role']}")
        data["excluded"] = keep
        save_excluded(data)
        print(f"\nRemoved {len(gone)} from the blocklist. They will reappear the next time tracker runs (if they still pass the filters)")
        return

    if not a.pattern:
        ap.print_help()
        return

    jobs = json.loads(JOBS.read_text(encoding="utf-8")) if JOBS.exists() else []
    seen = json.loads(SEEN.read_text(encoding="utf-8")) if SEEN.exists() else {}
    manual = ([j for j in json.loads(MANUAL_JOBS.read_text(encoding="utf-8"))
               if not j.get("_comment")] if MANUAL_JOBS.exists() else [])
    pat = a.pattern.strip()
    is_url = pat.startswith("http")
    low = pat.lower()

    def matches(company, role, url):
        return url == pat if is_url else low in f"{company} {role}".lower()

    # **Search both lists.** Searching only jobs.json, "Figma" would hit the real Figma
    # posting instead of the one just added manually, and with --yes would wrongly remove
    # a valid job (this actually happened).
    cands = []
    for j in jobs:
        if seen.get(j["key"], {}).get("status") == "shown" and \
                matches(j["company"], j["role"], j.get("url", "")):
            cands.append(("tracker", j))
    for j in manual:
        if matches(j["company"], j["role"], j.get("url", "")):
            cands.append(("manual", j))

    if not cands:
        print(f"No pending jobs match '{pat}'")
        return

    print(f"Found {len(cands)} matches:\n")
    for i, (src, j) in enumerate(cands, 1):
        tag = "Manually added" if src == "manual" else f"{j.get('score', '?')} pts"
        print(f"  {i}. [{tag}] {j['company']} — {j['role']}")
        print(f"     {(j.get('location') or 'Location unknown')[:60]}")
    if not a.yes:
        ans = input(f"\nWhich ones to remove? Enter = all, or type numbers (e.g. 1,3), n = cancel: ").strip().lower()
        if ans == "n":
            print("Cancelled")
            return
        if ans:
            try:
                pick = {int(x) for x in re.split(r"[,\s]+", ans) if x}
            except ValueError:
                print("Couldn't parse those numbers, cancelled")
                return
            cands = [c for i, c in enumerate(cands, 1) if i in pick]
            if not cands:
                print("No entries selected, cancelled")
                return

    removed_manual = [j for src, j in cands if src == "manual"]
    if removed_manual:
        rest = [j for j in manual if j not in removed_manual]
        header = {"_comment": "Manually added jobs. tracker.py loads this as the 5th source on every run, "
                              "and the force flag skips all hard filters. Maintained by add_job.py."}
        MANUAL_JOBS.write_text(
            json.dumps([header] + rest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        for j in removed_manual:
            print(f"  Removed from manually added list: {j['company']} — {j['role']}")

    blacklisted = 0
    for src, j in cands:
        # Manual entries are just deleted from their input file; no blacklist needed (they never come back from a source)
        if src == "manual":
            continue
        data["excluded"].append({
            "company": j["company"], "role": j["role"],
            "base": f"{norm_company(j['company'])}::{role_base(j['role'])}",
            "url": j.get("url", ""),
            "reason": a.reason, "removed": date.today().isoformat(),
        })
        if j["key"] in seen:
            seen[j["key"]]["status"] = "skipped"
            seen[j["key"]]["skip_reason"] = f"manually removed ({a.reason})"
        blacklisted += 1
    if blacklisted:
        save_excluded(data)
        SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  Added {blacklisted} to the blocklist")

    print(f"\nProcessed {len(cands)} in total. Run `python3 dashboard.py` to refresh the dashboard")


if __name__ == "__main__":
    main()
