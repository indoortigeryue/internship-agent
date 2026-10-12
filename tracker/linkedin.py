#!/usr/bin/env python3
"""LinkedIn job source: fetch internships posted in the last 24 hours into data/linkedin_jobs.json.

Runs **before** tracker.py, which loads it as its 6th source.
It is a separate script rather than part of tracker.fetch_all because this source
pages through results and fetches each posting's detail, which is much heavier than
the other sources; if it fails on its own, the main pipeline is unaffected.

## Uses the guest API, no login required

    /jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=..&location=..&f_TPR=r86400

10 results per page, giving: jobPosting ID, title, company, location, posted time.

## Repost detection (LinkedIn doesn't expose this)

The logged-in UI shows "Reposted 8 hours ago", but **the guest API has no such field** --
tested on a Samsara posting explicitly marked Reposted; the guest API only returned "8 hours ago".
The detail page doesn't have it either (even the company's real apply link is behind the
login wall; there are zero non-LinkedIn outbound links).

So we use three proxy signals, **all flag-only, never drop** (rule of thumb: a wrong drop is
worse than a wrong flag):

1. History dedup -- handled by tracker's seen.json / cross-source dedup, so the same
   company+title never shows up twice
2. Cross-source date conflict -- LinkedIn says hours ago, another source says weeks ago;
   tracker can spot this when merging
3. **Abnormal applicant count** -- posted hours ago but already lots of applicants is almost
   always a repost. The guest detail page exposes this (`41 applicants`); it's the only
   signal we can compute ourselves.

Usage:
    python3 linkedin.py              # fetch and write
    python3 linkedin.py --dry        # print only, don't write
    python3 linkedin.py --pages 3    # limit pages (default 40 pages ~ 400 results)
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import fetchlib  # noqa: E402
import verify  # noqa: E402  reuse its NO_SPONSOR / CITIZEN_ONLY phrases

# ---- Quality filter. LinkedIn postings are noticeably noisier than the GitHub sources,
# so they get an extra layer.
# **Every pattern must be phrase-anchored.** The first version used bare keywords, and
# Atlassian ("paid volunteer days"), KLA/Emerson ("tuition reimbursement") and
# Amex/Verisk ("developer bootcamp concepts") were all misclassified as unpaid -- 7 of 98 wrongly dropped.

UNPAID = [
    r"\bunpaid\s+(?:intern|internship|position|role)",
    r"(?:intern|internship|position)\s+is\s+unpaid",
    r"this\s+is\s+an?\s+unpaid",
    r"no\s+compensation\s+(?:is|will be)\s+(?:provided|offered|paid)",
    r"without\s+(?:any\s+)?(?:monetary\s+)?compensation",
    r"for\s+(?:academic\s+|college\s+)?credit\s+only",
    r"credit[-\s]only\s+internship",
    r"\bequity[-\s]only\b", r"\bcommission[-\s]only\b",
]
# "Internships" you pay for -- e.g. PM Corner Thailand's "Training Cum Internship"
PAY_TO_PLAY = [
    r"training\s+cum\s+internship", r"\btraining\s+fee", r"\bregistration\s+fee",
    r"enrollment\s+fee", r"self[-\s]funded",
    r"(?:fee|payment)\s+is\s+required\s+to\s+(?:enroll|join|participate)",
]
# Reliability signals: flag only, never drop
AGENCY = [r"\bstaffing\b", r"talent solutions", r"\brecruiting (?:agency|firm)\b",
          r"on behalf of our client", r"our client is seeking", r"\bconsultancy\b"]
CONTRACT = [r"\b1099\b", r"independent contractor", r"contract(?:-| )to(?:-| )hire"]

PAY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:-|–|to)?\s*\$?\s?([\d,]+(?:\.\d+)?)?\s*"
                    r"(?:per\s+|/\s*|an?\s+)?(hour|hr|year|yr|annually|month)", re.I)
MIN_HOURLY = 25


def hourly_range(jd):
    """Convert pay stated in the JD to an hourly range (low, high, raw text). None if not stated."""
    found = []
    for m in PAY_RE.finditer(jd or ""):
        a = float(m.group(1).replace(",", ""))
        b = float(m.group(2).replace(",", "")) if m.group(2) else a
        div = {"hour": 1, "hr": 1, "year": 2080, "yr": 2080,
               "annually": 2080, "month": 173}[m.group(3).lower()]
        vals = [v / div for v in (a, b)]
        if all(1 <= v <= 400 for v in vals):
            found.append((min(vals), max(vals), m.group(0).strip()))
    return min(found, key=lambda t: t[1]) if found else None


# Any mention of work authorization/visa counts as "addressed", whether positive or negative.
# Postings that never mention it are where "no sponsorship" surprises usually hide --
# rules can't catch them, so we just flag them as unverified.
MENTIONS_AUTH = re.compile(
    r"sponsor|visa|work authorization|authorized to work|citizenship|"
    r"\bcpt\b|\bopt\b|\bh-?1b\b|export control|u\.?s\.? person|green card|"
    r"permanent resident|immigration", re.I)


def quality_check(jd):
    """Return (drop reason or None, [soft flags]).

    Pay check uses the **top of the range**: drop only if even the max is under $25.
    Using the low end would wrongly drop normal ranges like Northrop's $20.5-$37.25
    or Eaton's $22.15-$37.5. A low end under $25 is only flagged for the user to review.
    """
    jd = jd or ""
    if any(re.search(p, jd, re.I) for p in PAY_TO_PLAY):
        return "pay-to-play 'internship'", []
    if any(re.search(p, jd, re.I) for p in UNPAID):
        return "unpaid position", []
    ev = verify.find_first(jd, verify.NO_SPONSOR) if verify.NEEDS_SPONSOR else None
    if ev:
        return "JD explicitly says no visa sponsorship", []
    ev = verify.find_first(jd, verify.CITIZEN_ONLY) if not verify.US_CITIZEN else None
    if ev:
        return "JD requires US citizenship / clearance", []

    rng = hourly_range(jd)
    if rng and rng[1] < MIN_HOURLY:
        return f"hourly pay tops out at only ${rng[1]:.0f} ({rng[2]})", []

    flags = []
    if verify.NEEDS_SPONSOR and not MENTIONS_AUTH.search(jd or ""):
        flags.append("JD never mentions work authorization — check the company site for sponsorship before applying")
    if rng and rng[0] < MIN_HOURLY:
        flags.append(f"low end of pay ${rng[0]:.0f} is on the low side ({rng[2]})")
    if len(jd) < 800:
        flags.append(f"JD is only {len(jd)} characters, too little info")
    if any(re.search(p, jd, re.I) for p in AGENCY):
        flags.append("looks like a staffing agency / recruiter repost")
    if any(re.search(p, jd, re.I) for p in CONTRACT):
        flags.append("may be contract / 1099")
    return None, flags

OUT = ROOT / "data" / "linkedin_jobs.json"

SEARCH = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
          "?keywords={kw}&location={loc}&f_TPR=r86400&start={start}")
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"

# The two title variants we search for. LinkedIn keyword search is fuzzy -- treat it as a wide net;
# the real filtering is tracker's is_swe(), so the rules live in one place and can't diverge.
QUERIES = ["Software Engineer Intern", "Software Engineering Intern"]
LOCATION = "United States"

PER_PAGE = 10
SLEEP = 2.5          # LinkedIn rate-limits aggressively; go slow between pages


def q(s):
    return s.replace(" ", "%20").replace(",", "%2C").replace("&", "%26")


def parse_cards(page_html):
    out = []
    for c in re.split(r'(?=<div class="base-card)', page_html)[1:]:
        def grab(pat, group=1):
            m = re.search(pat, c, re.S)
            return html.unescape(m.group(group)).strip() if m else ""
        jid = grab(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
        title = grab(r'base-search-card__title">\s*(.*?)\s*</h3>')
        if not jid or not title:
            continue
        out.append({
            "id": jid,
            "role": re.sub(r"\s+", " ", title),
            "company": re.sub(r"\s+", " ", grab(r'class="hidden-nested-link"[^>]*>\s*(.*?)\s*</a>')),
            "location": grab(r'job-search-card__location">\s*(.*?)\s*</span>'),
            "posted": grab(r'datetime="([^"]+)"'),
            "ago": grab(r'listdate[^>]*>\s*(.*?)\s*</time>'),
            "url": grab(r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)'),
        })
    return out


def hours_ago(txt):
    """'8 hours ago' -> 8; '1 day ago' -> 24; None if unrecognized."""
    m = re.match(r"(\d+)\s+(minute|hour|day|week)", txt.strip(), re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"minute": 1 / 60, "hour": 1, "day": 24, "week": 168}[unit]


def applicants(jid):
    """Applicant count from the detail page -- the only repost signal we can compute ourselves."""
    _, raw = fetchlib.http_get(DETAIL.format(jid=jid), timeout=25)
    if not raw:
        return None, ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    m = re.search(r"(\d[\d,]*)\s*applicants?", txt)
    n = int(m.group(1).replace(",", "")) if m else None
    # Also return the JD body so tracker/verify can check unpaid and sponsorship
    body = ""
    d = re.search(r'show-more-less-html__markup(.*?)</div>', raw, re.S)
    if d:
        body = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", d.group(1)))).strip()
    return n, body[:4000]


def repost_suspicion(hrs, n_applicants):
    """Posted hours ago but already lots of applicants -- almost certainly a repost (reposting
    resets LinkedIn's timestamp, but applicant counts accumulate). Returns (suspicious, note)."""
    if hrs is None or n_applicants is None:
        return False, ""
    if hrs <= 24 and n_applicants >= 100:
        return True, f"posted only {hrs:.0f} hours ago but already has {n_applicants} applicants, very likely a repost"
    if hrs <= 6 and n_applicants >= 25:
        return True, f"posted only {hrs:.0f} hours ago but already has {n_applicants} applicants, possibly a repost"
    return False, ""


def carry_over(kept):
    """Jobs from the previous linkedin_jobs.json that this run didn't return but that were
    posted within the last 24h, with hours_ago advanced to now."""
    if not OUT.exists():
        return []
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        then = datetime.fromisoformat(prev["fetched_at"])
    except (ValueError, KeyError):
        return []
    elapsed = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    have = {r["id"] for r in kept}
    out = []
    for r in prev.get("jobs", []):
        if r.get("id") in have or r.get("hours_ago") is None:
            continue
        age = r["hours_ago"] + elapsed
        if age <= 24:
            out.append({**r, "hours_ago": round(age, 1), "ago": f"{int(age)} hours ago", "carried_over": True})
    return out


def crawl(max_pages, dry):
    seen_ids, rows = set(), []
    for kw in QUERIES:
        print(f"\nKeyword \"{kw}\"")
        dup_streak = 0
        for page in range(max_pages):
            url = SEARCH.format(kw=q(kw), loc=q(LOCATION), start=page * PER_PAGE)
            status, body = fetchlib.http_get(url, timeout=30)
            # 429 = rate limited (LinkedIn does this after a few hundred guest requests, or
            # on a second run the same day). Back off and retry before giving up on the keyword.
            for wait in (20, 60):
                if status != 429:
                    break
                print(f"  page {page + 1}: HTTP 429, retrying in {wait}s")
                time.sleep(wait)
                status, body = fetchlib.http_get(url, timeout=30)
            if status != 200 or not body:
                print(f"  page {page + 1}: HTTP {status}, stopping pagination")
                break
            cards = parse_cards(body)
            fresh = [c for c in cards if c["id"] not in seen_ids]
            for c in fresh:
                seen_ids.add(c["id"])
            print(f"  page {page + 1}: {len(cards)} results, new: {len(fresh)}")
            rows += fresh
            if len(cards) < PER_PAGE:        # a short page means we've hit the end
                break
            # Stop only after two consecutive pages with nothing new. One page isn't enough --
            # LinkedIn occasionally returns the same page twice while paging, which would
            # stop a keyword early as if it had run out of data.
            dup_streak = dup_streak + 1 if not fresh else 0
            if dup_streak >= 2:
                print(f"  2 pages in a row with nothing new, stopping pagination")
                break
            time.sleep(SLEEP)

    # Keep only the last 24h (f_TPR sometimes lets older ones through)
    kept = []
    for r in rows:
        r["hours_ago"] = hours_ago(r["ago"])
        if r["hours_ago"] is not None and r["hours_ago"] > 24:
            continue
        kept.append(r)
    print(f"\n{len(rows)} total, {len(kept)} within 24 hours")

    # **Prefilter by title before fetching details.** 40 pages yields ~400 candidates;
    # fetching each detail at 1.2s apiece = 8 minutes. Most would fail is_swe anyway
    # (LinkedIn keyword search is fuzzy and returns lots of unrelated roles).
    # Running tracker's same rules first only skips requests that would be discarded.
    before = len(kept)
    try:
        import tracker as _t
        _prof = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
        kept = [r for r in kept if _t.is_swe(r["role"], _prof)]
        print(f"Title prefilter: {before} -> {len(kept)} (saved {before - len(kept)} detail requests)")
    except Exception as e:
        print(f"(prefilter skipped: {e})")

    # Fetch each detail page for applicant count + JD body
    print("Fetching details (applicant count / JD)…")
    for i, r in enumerate(kept, 1):
        n, body = applicants(r["id"])
        r["applicants"] = n
        r["jd"] = body
        r["repost_suspect"], r["repost_note"] = repost_suspicion(r["hours_ago"], n)
        r["drop_reason"], r["quality_flags"] = quality_check(body)
        rng = hourly_range(body)
        r["pay_range"] = f"${rng[0]:.0f}-${rng[1]:.0f}/hr" if rng else ""
        if i % 10 == 0 or i == len(kept):
            print(f"  {i}/{len(kept)}")
        time.sleep(1.2)

    dropped_q = [r for r in kept if r.get("drop_reason")]
    kept = [r for r in kept if not r.get("drop_reason")]
    if dropped_q:
        print(f"\nQuality filter dropped {len(dropped_q)}:")
        for r in dropped_q:
            print(f"  ✗ {r['company'][:24]:24} | {r['role'][:34]:34} | {r['drop_reason']}")
    flagged = [r for r in kept if r.get("quality_flags")]
    if flagged:
        print(f"\nSoft-flagged but kept {len(flagged)}:")
        for r in flagged:
            print(f"  ⚑ {r['company'][:24]:24} | {'; '.join(r['quality_flags'])}")

    sus = [r for r in kept if r["repost_suspect"]]
    print(f"\nSuspected reposts: {len(sus)} found")
    for r in sus[:6]:
        print(f"  ⚠️ {r['company']} — {r['role'][:46]} · {r['repost_note']}")

    # **No accumulation**: the pool holds only this run's results. The search itself is
    # f_TPR=r86400 (last 24h), so the file only ever contains jobs posted <= 24 hours ago.
    #
    # An earlier version merged prior results and kept them N days from "first seen" --
    # i.e. N days after entering the pool, so yesterday's jobs were still there today at 24-47h old.
    # The cutoff should be 24h **by posting time**, not pool entry time, so the accumulation logic was removed.
    #
    # Cost: if the pipeline skips a day, that day's LinkedIn jobs are missed for good. This is an accepted trade-off.
    today = datetime.now(timezone.utc).date()
    for r in kept:
        r["first_seen"] = today.isoformat()

    # ...but a second run the same day must not wipe out the first one. LinkedIn
    # rate-limits a repeat crawl after a page or two, so overwriting would replace a full
    # morning crawl with a truncated one (2026-10-12: 25 kept jobs became 13). Carry over
    # earlier jobs that are *still* within 24h of posting -- the cutoff stays by posting time.
    carried = carry_over(kept)
    if carried:
        print(f"Carried over {len(carried)} jobs from the previous run (still within 24h of posting)")
        kept += carried

    if dry:
        print("\n(--dry, nothing written)")
        for r in kept[:12]:
            print(f"  {r['ago']:>14} | {r['company'][:20]:20} | {r['role'][:50]}")
        return kept

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_comment": "New jobs from the last 24h, scraped via the LinkedIn guest API; tracker.py loads this as its 6th source. "
                    "Generated by linkedin.py; do not edit by hand.",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "jobs": kept,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    oldest = max((r["hours_ago"] for r in kept if r.get("hours_ago") is not None), default=0)
    print(f"\nWrote {OUT} ({len(kept)} jobs, oldest posted {oldest:.0f} hours ago)")
    return kept


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=40)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    crawl(a.pages, a.dry)
