#!/usr/bin/env python3
"""Manually add a job: paste an application link and it scrapes the company, title,
location and salary, then adds it to the to-apply list.

Automatic filtering always misses things; this is the escape hatch. Manually added jobs
carry a `force` flag and **skip all hard filters** (15-day freshness, track match, term,
etc.) -- pasting a link means the user has already reviewed it and wants to apply, so the
filters shouldn't second-guess that.

Stored in manual_jobs.json and reloaded as the 5th source on every tracker.py run, so
nothing is lost.

Usage:
    python3 add_job.py <url>
    python3 add_job.py <url> --company Stripe --role "Backend Intern"   # specify manually
    python3 add_job.py <url> --note "referral"
    python3 add_job.py --list                                          # show manually added jobs
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
MANUAL = ROOT / "manual_jobs.json"
sys.path.insert(0, str(ROOT))
import verify  # noqa: E402  reuse its fetching, Oracle API, body extraction, real title


# Guess company from the domain; fallback when no title can be scraped
HOST_HINTS = {
    "greenhouse.io": None, "ashbyhq.com": None, "lever.co": None,
    "myworkdayjobs.com": None, "icims.com": None, "simplify.jobs": None,
}


def guess_company(url, raw, text):
    """Prefer structured data in the page, then the company segment of the ATS path, then the domain."""
    for pat in (r'"hiringOrganization"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]{2,40})"',
                r'"CompanyName"\s*:\s*"([^"]{2,40})"',
                r'"companyName"\s*:\s*"([^"]{2,40})"'):
        m = re.search(pat, raw, re.I | re.S)
        if m:
            return m.group(1).strip()

    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path
    # Greenhouse/Ashby/Lever put the company in the first path segment: /<company>/jobs/123
    if any(h in host for h in ("greenhouse.io", "ashbyhq.com", "lever.co", "careerpuck.com")):
        parts = [p for p in path.split("/") if p and p not in ("embed", "job_app", "jobs")]
        if parts:
            return parts[0].replace("-", " ").title()
    # Workday: <company>.wdN.myworkdayjobs.com
    m = re.match(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", host)
    if m:
        return m.group(1).replace("-", " ").title()
    # Fallback: main domain with www and suffixes stripped
    parts = [p for p in host.split(".") if p not in ("www", "com", "jobs", "careers", "io", "co")]
    return parts[-1].title() if parts else host


def guess_location(raw, text):
    for pat in (r'"jobLocation"\s*:.*?"addressLocality"\s*:\s*"([^"]{2,40})"[^}]*?'
                r'"addressRegion"\s*:\s*"([^"]{2,30})"',
                r'"PrimaryLocation"\s*:\s*"([^"]{2,60})"',
                r'"primaryLocation"\s*:\s*"([^"]{2,60})"'):
        m = re.search(pat, raw, re.I | re.S)
        if m:
            return ", ".join(g for g in m.groups() if g)
    m = re.search(r"\b(Remote(?:\s+in\s+\w+)?)\b", text)
    return m.group(1) if m else ""


def guess_salary(text):
    m = re.search(r"\$[\d,.]+(?:\s*[-–]\s*\$[\d,.]+)?\s*/\s*(?:hr|hour|yr|year)", text, re.I)
    return re.sub(r"\s+", " ", m.group(0)) if m else ""


def load():
    if not MANUAL.exists():
        return []
    return [j for j in json.loads(MANUAL.read_text(encoding="utf-8"))
            if not j.get("_comment")]


def save(items):
    header = {"_comment": "Manually added jobs. tracker.py loads this as a 5th source on every rerun; "
                          "the force flag skips all hard filters. Maintained by add_job.py."}
    MANUAL.write_text(json.dumps([header] + items, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="job application URL")
    ap.add_argument("--company")
    ap.add_argument("--role")
    ap.add_argument("--location")
    ap.add_argument("--note", default="", help="note, shown on the dashboard")
    ap.add_argument("--list", action="store_true", help="list manually added jobs")
    a = ap.parse_args()

    items = load()

    if a.list or not a.url:
        if not items:
            print("No manually added jobs yet")
            return
        print(f"Manually added jobs ({len(items)}):\n")
        for i, j in enumerate(items, 1):
            print(f"  {i}. {j['company']} — {j['role']}")
            print(f"     {j.get('location') or 'location unknown'}"
                  + (f" · {j['salary']}" if j.get("salary") else "")
                  + f" · added {j.get('added', '?')}")
            if j.get("note"):
                print(f"     Note: {j['note']}")
            print(f"     {j['url']}")
        return

    url = a.url.strip()
    if any(j["url"] == url for j in items):
        print("This URL has already been added")
        return

    print(f"Fetching {url} …")
    status, raw, api_title, meta = verify.fetch_jd(url)

    text, title = "", api_title
    if raw and not raw.startswith("__ERROR__"):
        text = verify.page_text(raw)
        title = api_title or verify.real_title(raw)
        print(f"  HTTP {status}, JD {len(text):,} chars"
              + (" (via API)" if api_title else ""))
    else:
        print(f"  ⚠️ Could not fetch page content ({status or raw[:60]}) - you'll need to specify the company and role manually")

    company = a.company or guess_company(url, raw, text)
    role = a.role or title or "(role title unknown)"
    location = a.location or meta.get("location") or guess_location(raw, text)
    salary = guess_salary(text)

    # If the JD was fetched, check for sponsorship language -- warn only, never block
    warn = ""
    if len(text) > 400:
        for label, pats in (("The JD seems to say sponsorship is not offered", verify.NO_SPONSOR),
                            ("The JD seems to require citizenship / security clearance", verify.CITIZEN_ONLY)):
            ev = verify.find_first(text, pats)
            if ev:
                warn = f"{label}: …{ev[:160]}…"
                break

    items.append({
        "company": company, "role": role, "location": location,
        "url": url, "salary": salary, "note": a.note,
        "added": date.today().isoformat(),
        "requirements": [text[:4000]] if text else [],
    })
    save(items)

    print(f"\nAdded:")
    print(f"  Company: {company}")
    print(f"  Role: {role}")
    print(f"  Location: {location or '(unknown)'}")
    if salary:
        print(f"  Salary: {salary}")
    if warn:
        print(f"\n  ⚠️ {warn}")
        print("  (Added anyway - manual additions are never blocked, use your own judgment)")
    if not a.company and company:
        print(f"\nThe company name was guessed automatically; if it's wrong, re-add with --company (remove this entry with remove_job.py first)")
    print(f"\nRun `bash run_daily.sh` or `python3 tracker.py && python3 dashboard.py` and it will show up on the dashboard")


if __name__ == "__main__":
    main()
