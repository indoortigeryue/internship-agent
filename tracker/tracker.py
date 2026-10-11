#!/usr/bin/env python3
"""Daily internship tracker: fetch 4 sources -> dedupe -> filter & score against the profile -> write a daily report.

Usage:
    python3 tracker.py                      # daily incremental run
    python3 tracker.py --baseline           # first full run (treat every job as new)
    python3 tracker.py --applied "Acme"     # mark matching jobs as applied
    python3 tracker.py --skip "Oracle"      # mark as skipped; never remind again
"""

import argparse
import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import fetchlib

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"
REPORTS = ROOT / "reports"
SEEN_FILE = DATA / "seen.json"
JOBS_FILE = DATA / "jobs.json"
LINKEDIN_JOBS = DATA / "linkedin_jobs.json"   # written by linkedin.py, which runs before the tracker
MANUAL_JOBS = ROOT / "manual_jobs.json"          # manually added jobs (written by add_job.py)
MANUAL_EXCLUDED = ROOT / "manual_excluded.json"  # manually removed jobs (written by remove_job.py)
PROFILE_FILE = ROOT / "profile.json"  # the user's own profile, written by `python3 setup.py`


def load_profile():
    if not PROFILE_FILE.exists():
        sys.exit("No tracker/profile.json yet. Run `python3 setup.py` from the repo root first.")
    return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))


_P = json.loads(PROFILE_FILE.read_text(encoding="utf-8")) if PROFILE_FILE.exists() else {}
SEASON = _P.get("season_year", 2027)   # which year's internship lists to pull

SOURCES = {
    "simplify": f"https://raw.githubusercontent.com/SimplifyJobs/Summer{SEASON}-Internships/dev/README.md",
    "vansh": f"https://raw.githubusercontent.com/vanshb03/Summer{SEASON}-Internships/dev/README.md",
    "vansh_offseason": f"https://raw.githubusercontent.com/vanshb03/Summer{SEASON}-Internships/dev/OFFSEASON_README.md",
    "faang": "https://simplify.jobs/top-list/FAANG-Software-Internships",
}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
MARKERS = ["🛂", "🇺🇸", "🔒", "🔥", "🎓"]
# Priority when picking a URL; lower = more trustworthy
# manual comes first: a manually pasted link is exactly the one the user wants and must not be overridden
SOURCE_URL_RANK = {"manual": -1, "faang": 0, "simplify": 1, "vansh": 2,
                   "vansh_offseason": 3, "linkedin": 4}
# LinkedIn comes last: its links take an extra redirect to reach the company page, so prefer direct links

# The user's first-choice area gets its own report section regardless of score.
# setup.py writes it as profile.candidate.preferred_area; LA is the fallback.
# For LA the criterion is "commutable", not "within city limits": Orange County is included, and
# the San Fernando Valley ones (sherman oaks / van nuys / ...) are inside LA city -- missing them
# drops jobs like Activision out of the section.
DEFAULT_LA = ["los angeles", "el segundo", "santa monica", "culver city", "pasadena",
             "burbank", "playa vista", "glendale", "torrance", "marina del rey",
             "hawthorne", "westwood", "irvine", "long beach", "costa mesa", "anaheim",
             "sherman oaks", "van nuys", "woodland hills", "north hollywood",
             "universal city", "beverly hills", "west hollywood", "calabasas",
             "manhattan beach", "redondo beach", "inglewood", "thousand oaks"]
PREF_AREA = _P.get("candidate", {}).get("preferred_area") or {"label": "Los Angeles", "cities": DEFAULT_LA}
PREF_LABEL = PREF_AREA["label"]
PREF_CITIES = [c.lower() for c in PREF_AREA["cities"]]


# ---------------------------------------------------------------- fetching

def fetch(url, timeout=60):
    """Delegate to fetchlib -- it falls back to curl when urllib fails on an old SSL stack."""
    status, text = fetchlib.http_get(url, timeout=timeout)
    if status and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    if not text:
        raise RuntimeError("Could not fetch content")
    return text


def fetch_all(today):
    """Fetch all 4 sources and save a snapshot of each (the FAANG page needs snapshot diffs to detect new jobs)."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, url in SOURCES.items():
        try:
            text = fetch(url)
            (SNAPSHOTS / f"{today}_{name}.txt").write_text(text, encoding="utf-8")
            out[name] = text
            print(f"  ✓ {name}: {len(text):,} bytes")
        except Exception as e:  # one failing source doesn't affect the others
            print(f"  ✗ {name}: {e}", file=sys.stderr)
            out[name] = None
    return out


# ---------------------------------------------------------------- parsing helpers

def strip_tags(s):
    s = re.sub(r"<br\s*/?>|</br>", " / ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def pick_apply_url(cell):
    """Pick the real company application link from the Application cell; fall back to the Simplify link."""
    urls = re.findall(r'href="([^"]+)"', cell)
    for u in urls:
        if "simplify.jobs" not in u:
            return html.unescape(u)
    return html.unescape(urls[0]) if urls else ""


def extract_markers(*cells):
    blob = " ".join(cells)
    return [m for m in MARKERS if m in blob]


def clean_text(s):
    for m in MARKERS:
        s = s.replace(m, "")
    return re.sub(r"\s+", " ", s).strip()


def clean_location(s):
    """Strip the '**30 locations**' summary prefix from Simplify multi-location cells."""
    s = re.sub(r"^\*{0,2}\s*\d+\s+locations?\s*\*{0,2}\s*", "", clean_text(s), flags=re.I)
    return s.strip(" /")


def parse_age(s):
    """'0d' / '3mo' / '1y' -> number of days."""
    m = re.match(r"(\d+)\s*(d|mo|y)", s.strip(), re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"d": 1, "mo": 30, "y": 365}[unit]


def parse_posted_date(s, today):
    """'Aug 21' -> number of days (no year given; infer the latest year not after today)."""
    m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})", s.strip())
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", "%b %d %Y").date()
    except ValueError:
        return None
    if d > today:
        d = d.replace(year=today.year - 1)
    return (today - d).days


# ---------------------------------------------------------------- per-source parsers

def parse_simplify(text):
    """SimplifyJobs README: categorized <table>s, 5 columns, '↳' means same company as the previous row."""
    rows = []
    # split into sections first so the category is kept
    sections = re.split(r"\n##\s+(.+?)\n", text)
    chunks = [("Other", sections[0])] + [
        (clean_text(sections[i]), sections[i + 1]) for i in range(1, len(sections) - 1, 2)
    ]
    for section, body in chunks:
        last_company = ""
        for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 5:
                continue
            company_raw, role_raw, loc_raw, app_raw, age_raw = tds[:5]
            company = clean_text(strip_tags(company_raw))
            if company in ("↳", ""):
                company = last_company
            else:
                last_company = company
            role = clean_text(strip_tags(role_raw))
            if not company or not role:
                continue
            rows.append({
                "company": company,
                "role": role,
                "location": clean_location(strip_tags(loc_raw)),
                "url": pick_apply_url(app_raw),
                "age_days": parse_age(strip_tags(age_raw)),
                "markers": extract_markers(company_raw, role_raw),
                "section": section,
                "source": "simplify",
            })
    return rows


def parse_vansh(text, source, today):
    """vanshb03 README: markdown table, 5 columns, '↳' also means same company."""
    rows = []
    last_company = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("Company", "-------") or set(cells[0]) <= {"-", " "}:
            continue
        company_raw, role_raw, loc_raw, app_raw, date_raw = cells[:5]
        company = clean_text(strip_tags(company_raw))
        if company in ("↳", ""):
            company = last_company
        else:
            last_company = company
        role = clean_text(strip_tags(role_raw))
        if not company or not role:
            continue
        rows.append({
            "company": company,
            "role": role,
            "location": clean_location(strip_tags(loc_raw)),
            "url": pick_apply_url(app_raw),
            "age_days": parse_posted_date(date_raw, today),
            "markers": extract_markers(company_raw, role_raw),
            "section": "Off-season / Co-op" if "offseason" in source else "Mixed list",
            "source": source,
        })
    return rows


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_grad_window(requirements):
    """Extract the graduation-date requirement from JD requirements; return (earliest, latest, sentence).

    These phrasings mean very different things; conflating them causes false rejects:
      'graduation between December 2027 and June 2028'  -> closed interval
      'graduating by August 2028'                       -> upper bound only
      'graduating in 2029 or later'                     -> lower bound only
      'completed your sophomore year ... undergraduate' -> not a graduation requirement; skip
    Note 'undergraduate' also contains 'graduat' and must be excluded, or many sentences get misread.
    """
    GRAD = re.compile(r"(?<!under)graduat", re.I)
    for req in requirements or []:
        if not GRAD.search(req):
            continue
        low = req.lower()
        years = []
        for m in re.finditer(r"(?:([A-Za-z]{3,9})\s+)?(20\d\d)", req):
            mon = MONTHS.get((m.group(1) or "")[:3].lower())
            years.append((int(m.group(2)), mon))
        if not years:
            continue
        if re.search(r"graduat\w*\s+(?:by|before|no later than|prior to)|"
                     r"\bby\b\s+(?:[a-z]+\s+)?20\d\d", low):
            lo, hi = (1900, 1), max((y, mo or 12) for y, mo in years)
        elif re.search(r"or later|or after|onwards|or beyond|at the earliest", low):
            lo, hi = min((y, mo or 1) for y, mo in years), (2100, 12)
        else:
            lo = min((y, mo or 1) for y, mo in years)
            hi = max((y, mo or 12) for y, mo in years)
        return lo, hi, req.strip()
    return None


def grad_fit(requirements, profile):
    """Compare the JD's graduation window with the candidate's graduation range -> 'fit' / 'miss' / None (no requirement)."""
    win = parse_grad_window(requirements)
    if not win:
        return None, ""
    lo, hi, sentence = win
    g = profile["candidate"]
    my_lo = tuple(int(x) for x in g["graduation_earliest"].split("-"))
    my_hi = tuple(int(x) for x in g["graduation_latest"].split("-"))
    overlap = my_lo <= hi and lo <= my_hi          # any overlap counts as a fit
    return ("fit" if overlap else "miss"), sentence


def parse_faang(html_text):
    """simplify.jobs FAANG list.

    The page collapses most jobs behind 'Show X more jobs', but expanding makes **no network request** --
    so all the data is already in the __NEXT_DATA__ JSON (1177 entries) and plain Python gets everything,
    no browser needed. This data is much richer than the two GitHub READMEs:
    posted date, hourly pay, H1B sponsorship, full requirements (including graduation window).
    """
    if not html_text:
        return []
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return []
    try:
        hits = json.loads(m.group(1))["props"]["pageProps"]["initialJobHits"]
    except (ValueError, KeyError):
        return []

    out = []
    for j in hits:
        company = ((j.get("job") or {}).get("company") or {}).get("name", "")
        title = j.get("title") or (j.get("job") or {}).get("title", "")
        if not company or not title:
            continue
        locs = [l.get("value", "") for l in (j.get("locations") or [])]
        salary = ""
        if j.get("min_salary"):
            def fmt(v):
                return f"{round(float(v)):g}" if float(v) >= 10 else f"{float(v):.2f}"
            lo_s, hi_s = j["min_salary"], j.get("max_salary")
            salary = f"${fmt(lo_s)}" + (f" - ${fmt(hi_s)}" if hi_s and hi_s != lo_s else "") + "/hr"
        out.append({
            "company": company,
            "role": title,
            "location": " / ".join(locs),
            "url": j.get("url", ""),
            "posted": (j.get("start_date") or j.get("updated_date") or "")[:10],
            "active": bool(j.get("active")),
            "salary": salary,
            "sponsors_h1b": j.get("sponsors_h1b"),
            "requirements": j.get("requirements") or [],
            "id": j.get("id", ""),
        })
    return out


def faang_to_jobs(faang_rows, today):
    """Merge **currently open** FAANG-list jobs into the main pipeline, deduped together with the two READMEs."""
    rows = []
    for f in faang_rows:
        if not f["active"]:
            continue
        age = None
        if f["posted"]:
            try:
                age = (today - datetime.strptime(f["posted"], "%Y-%m-%d").date()).days
            except ValueError:
                pass
        rows.append({
            "company": f["company"], "role": f["role"],
            "location": clean_location(f["location"]),
            "url": f["url"], "age_days": age, "markers": ["🔥"],
            "section": "FAANG list", "source": "faang",
            "salary": f["salary"], "sponsors_h1b": f["sponsors_h1b"],
            "requirements": f["requirements"],
        })
    return rows


# ---------------------------------------------------------------- filtering & scoring

# The same job can have completely different URLs across sources, but the ATS job ID is the same.
# E.g. a Dropbox job is boards.greenhouse.io/embed/job_app?token=8106224 in simplify,
# and job-boards.greenhouse.io/dropbox/jobs/8106224 when added manually -- same 8106224.
# The titles differ too (Software Engineering Intern vs Software Engineer Intern),
# so title-only dedup misses it; an ID-based layer is required.
ATS_ID_PATTERNS = [
    (r"greenhouse\.io/(?:embed/job_app\?token=|[a-z0-9_-]+/jobs/)(\d+)", "gh"),
    (r"gh_jid=(\d+)", "gh"),
    (r"ashbyhq\.com/[a-z0-9_.-]+/([0-9a-f-]{36})", "ashby"),
    (r"oraclecloud\.com/.*?/job/(\d+)", "oracle"),
    (r"myworkdayjobs\.com/.*?_(R-?\d[\w-]*)", "wd"),
    (r"jobs\.lever\.co/[a-z0-9_-]+/([0-9a-f-]{36})", "lever"),
    (r"icims\.com/jobs/(\d+)", "icims"),
    (r"simplify\.jobs/jobs/click/([0-9a-f-]{36})", "simplify"),
]


def ats_id(url):
    """Extract the ATS job ID from a URL; None if not found."""
    if not url:
        return None
    for pat, kind in ATS_ID_PATTERNS:
        m = re.search(pat, url, re.I)
        if m:
            return f"{kind}:{m.group(1).lower()}"
    return None


SEASON_RE = re.compile(r"\b(summer|winter|spring|fall|autumn)\b", re.I)


def role_base(role):
    """Role title with term and year removed, used for cross-source dedup.

    The same job listed as 'Software Engineer Intern' and
    'Software Engineer Intern - Summer 2027' by two sources must be merged.
    But **only strip term and year**, not intern / co-op -- Amazon's
    'SDE Intern' and 'SDE Intern/Co-op' may be different reqs and would be wrongly merged.
    """
    r = SEASON_RE.sub(" ", role)
    r = re.sub(r"\b20\d\d\b", " ", r)
    return re.sub(r"[^a-z0-9]+", "", r.lower())


def seasons_of(role):
    return {m.group(1).lower() for m in SEASON_RE.finditer(role or "")}


def norm_key(company, role, seasons=None):
    c = re.sub(r"[^a-z0-9]+", "", company.lower())
    base = f"{c}::{role_base(role)}"
    ss = seasons if seasons is not None else seasons_of(role)
    return f"{base}::{'|'.join(sorted(ss))}" if ss else base


# US state codes, used to check whether a multi-location job includes the US
US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
                "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA "
                "WA WV WI WY DC PR".split())
CA_PROVINCES = set("ON BC QC AB MB SK NS NB NL PE YT NT NU".split())


def canada_only(location):
    """Exclude Canada-only jobs (the candidate needs US sponsorship; Canada is a separate work-permit system),
    but keep multi-location jobs that also include the US -- Microsoft Vancouver+Redmond,
    Superhuman Toronto+Seattle+SF+NYC are all applicable."""
    if not location:
        return False
    low = location.lower()
    codes = set(re.findall(r",\s*([A-Z]{2})\b", location))
    has_us = bool(codes & US_STATES) or bool(
        re.search(r"\busa\b|united states|remote in us|remote - us|\bu\.s\.", low))
    has_ca = bool(codes & CA_PROVINCES) or "canada" in low
    return has_ca and not has_us


def is_swe(role, profile):
    r = role.lower()
    ex = profile["hard_exclude"]
    if any(k in r for k in ex["non_swe_role_keywords"]):
        # When the title has a clear backend/Java/full-stack signal, the AI-ML group doesn't apply --
        # 'Backend Software Engineer Intern - Java & AI - Masters' is one of the best-fit roles
        if not any(re.search(p, r) for p in ex.get("swe_override_regex", [])):
            return False
    if any(re.search(p, r) for p in ex.get("non_swe_role_regex", [])):
        if not any(re.search(p, r) for p in ex.get("swe_override_regex", [])):
            return False
    if any(k in r for k in profile["swe_include_keywords"]):
        return True
    # Position-independent fallback: "software ... intern" phrasings without "engineer".
    # Kept last -- the exclusion rules above have already run, so this bypasses nothing.
    return any(re.search(p, r) for p in profile.get("swe_include_regex", []))


def reject_reason(job, profile, today):
    """Return the rejection reason; None means it passes."""
    ex = profile["hard_exclude"]
    for m in ex["markers"]:
        if m in job["markers"]:
            return {"🛂": "No sponsorship", "🇺🇸": "US citizenship required", "🔒": "Closed"}[m]
    text = f"{job['company']} {job['role']}".lower()
    for kw in ex["role_keywords"]:
        if kw in text:
            return f"Security clearance/export control ({kw})"
    for c in ex["companies"]:
        if c.lower() in job["company"].lower():
            return f"Defense-related ({c})"
    if not is_swe(job["role"], profile):
        return "Not a SWE role"
    deg = profile["candidate"].get("degree_level", "master")
    undergrad = any(re.search(p, job["role"], re.I) for p in ex.get("undergrad_only_regex", []))
    # undergraduate and graduate differ only by prefix; the regex must anchor on \bundergrad
    if undergrad and deg != "bachelor":
        return "Undergrad only"
    if deg == "bachelor" and not undergrad and ("🎓" in job["markers"] or re.search(
            r"\bmaster'?s?\b|graduate[\s-]level|\bph\.?d\b", job["role"], re.I)):
        return "Graduate degree required"
    if deg == "master" and re.search(r"\bph\.?d\b", job["role"], re.I):
        return "PhD only"
    if ex.get("exclude_canada_only") and canada_only(job["location"]):
        return "Canada only"
    if ex.get("exclude_no_h1b_sponsor") and job.get("sponsors_h1b") is False:
        return "Company doesn't sponsor H1B"
    if ex.get("exclude_grad_window_mismatch"):
        fit, _ = grad_fit(job.get("requirements"), profile)
        if fit == "miss":
            return "graduation date mismatch"
    mism = jd_mismatch(job, profile)
    if mism:
        return mism
    cap = ex.get("max_age_days")
    if cap is not None:
        age = job.get("age_days")
        if age is None:
            return "No posting date"
        if age > cap:
            return f"Posted over {cap} days ago"
    return None


TERM_RE = re.compile(r"\b(summer|winter|spring|fall|autumn)\b[\s\-/]{0,3}(20\d\d)", re.I)


def stale_term(job, profile):
    """Exclude if the term stated in the title or URL is already past.

    The off-season README still lists jobs like 'Winter 2026' whose links 404.
    Pass if any term >= min_term_year appears, to avoid hurting jobs with no year.
    """
    min_year = profile["hard_exclude"].get("min_term_year")
    if not min_year:
        return None
    blob = f"{job['role']} {job.get('url', '')}"
    years = [int(y) for _, y in TERM_RE.findall(blob)]
    if years and max(years) < min_year:
        return f"Term already past ({max(years)})"
    # Terms the candidate can't do. Only when the title/URL states a term; no term = keep.
    avail = {s for t in profile["candidate"].get("available_terms", [])
             for s in ("summer", "fall", "spring", "winter") if s in t.lower()}
    stated = {t.lower().replace("autumn", "fall") for t, _ in TERM_RE.findall(blob)}
    if avail and stated and not stated & avail:
        return f"Term not available ({'/'.join(sorted(stated))})"
    return None


def load_linkedin_jobs(today):
    """LinkedIn source, fetched separately by linkedin.py into data/linkedin_jobs.json.

    **Only jobs posted within 24 hours, no accumulation** -- rewritten each run, so the pool only holds the latest fetch.
    The 24-hour cutoff is by **posting time**, not by time added to the pool plus N days.

    **No force flag** -- goes through exactly the same filters as other sources.
    LinkedIn keyword search is fuzzy and returns lots of irrelevant jobs; is_swe() does the filtering.
    """
    if not LINKEDIN_JOBS.exists():
        return []
    try:
        data = json.loads(LINKEDIN_JOBS.read_text(encoding="utf-8"))
    except ValueError:
        return []
    rows = []
    for j in data.get("jobs", []):
        # the pool only holds jobs posted within 24h (linkedin.py doesn't accumulate), so age is always 0
        hrs = j.get("hours_ago")
        age = 0 if hrs is None or hrs < 24 else int(hrs // 24)
        rows.append({
            "company": j.get("company", ""), "role": j.get("role", ""),
            "location": clean_location(j.get("location", "")),
            "url": j.get("url", ""), "age_days": age, "markers": [],
            "section": "LinkedIn", "source": "linkedin",
            "requirements": [j["jd"]] if j.get("jd") else [],
            "repost_suspect": bool(j.get("repost_suspect")),
            "repost_note": j.get("repost_note", ""),
            "applicants": j.get("applicants"),
            "quality_flags": j.get("quality_flags") or [],
            "pay_range": j.get("pay_range", ""),
        })
    return rows


def load_manual_jobs(today):
    """Load manually added jobs as a 5th source.

    Must live in a separate file reloaded on every run -- the tracker regenerates jobs.json from the 4 sources daily,
    so an entry only stuffed into seen.json would vanish the next day.

    These jobs carry a `force` flag: **skip all hard filters** (freshness, role type, term, etc.).
    A manually pasted link means the user has already reviewed it and will apply; filters shouldn't override that.
    """
    if not MANUAL_JOBS.exists():
        return []
    rows = []
    for j in json.loads(MANUAL_JOBS.read_text(encoding="utf-8")):
        if j.get("_comment"):
            continue
        age = None
        if j.get("added"):
            try:
                age = (today - datetime.strptime(j["added"], "%Y-%m-%d").date()).days
            except ValueError:
                pass
        rows.append({
            "company": j["company"], "role": j["role"],
            "location": clean_location(j.get("location", "")),
            "url": j.get("url", ""), "age_days": age, "markers": [],
            "section": "Manually added", "source": "manual", "force": True,
            "salary": j.get("salary", ""), "requirements": j.get("requirements", []),
            "note": j.get("note", ""),
        })
    return rows


def excluded_manually(job):
    """Manually removed jobs must not reappear from the sources the next day.

    Match on company + term-stripped title rather than key -- the key changes whenever upstream edits the title,
    so key-based matching would miss it. The URL is recorded too as a backup.
    """
    if not MANUAL_EXCLUDED.exists():
        return None
    data = json.loads(MANUAL_EXCLUDED.read_text(encoding="utf-8"))
    c = re.sub(r"[^a-z0-9]+", "", job["company"].lower())
    base = f"{c}::{role_base(job['role'])}"
    for e in data.get("excluded", []):
        if e.get("base") == base or (e.get("url") and e["url"] == job.get("url")):
            return f"Manually removed ({e.get('reason', 'not interested')})"
    return None


def cooldown_block(job, profile, today):
    """Application-quota cooldown: for companies like TikTok/ByteDance that allow only 2 applications per 6 months, hide them until the cooldown expires once the quota is used."""
    for g in profile.get("cooldown", {}).get("groups", []):
        if not any(c.lower() in job["company"].lower() for c in g["companies"]):
            continue
        y, m, d = (int(x) for x in g["last_applied"].split("-"))
        m += g["months"]
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        until = date(y, m, min(d, 28))
        if today < until:
            return f"Application quota cooldown (hidden until {until})"
    return None


ML_WORDS = ["pytorch", "tensorflow", "model training", "transformer", "neural network",
            "deep learning", "machine learning", "llm", "fine-tun", "inference",
            "computer vision", "nlp", "reinforcement learning", "embedding"]
MY_LANGS = ["python", "java", "javascript", "typescript", "sql", "web", "backend",
            "back-end", "full stack", "full-stack", "api", "database", "cloud"]


def jd_mismatch(job, profile):
    """When the full JD is available (the simplify source has requirements), check whether the focus is too far off.

    Only applies to jobs with a JD; those without rely on title filtering and aren't affected by this layer.
    """
    rules = profile["hard_exclude"].get("jd_mismatch", {})
    reqs = job.get("requirements") or []
    if not reqs:
        return None
    blob = " ".join(reqs).lower()
    langs = MY_LANGS + [x.lower() for x in profile["candidate"].get("stack", []) if x.lower() != "c++"]
    if rules.get("cpp_only") and re.search(r"\bc\+\+", blob) and not any(k in blob for k in langs):
        return "C++-only role (JD never mentions Python/Java/Web)"
    if rules.get("ml_heavy") and sum(blob.count(w) for w in ML_WORDS) >= 3:
        return "AI/ML role (JD heavy on ML requirements)"
    return None


def itar_risk(job, profile):
    """Soft warning: aerospace/defense internships often require 'must be a U.S. person', but the JD may not say so."""
    warn = profile.get("soft_warn", {})
    text = f"{job['company']} {job['role']}".lower()
    for c in warn.get("companies", []):
        if c.lower() in job["company"].lower():
            return True
    return any(k in text for k in warn.get("keywords", []))


def score(job, profile, faang_map):
    sc = profile["scoring"]
    total, why = 0, []

    age = job.get("age_days")
    if age is not None:
        for label, pts in sc["freshness"].items():
            lo, hi = (int(x) for x in label.replace("d", "").split("-"))
            if lo <= age <= hi:
                total += pts
                why.append(f"fresh ({age}d)" if age <= 1 else f"{age}d ago")
                break

    loc = job["location"].lower()
    best_loc = max(((pts, name) for name, pts in sc["locations"].items() if name in loc),
                   default=(0, ""))
    if best_loc[0]:
        total += best_loc[0]
        if best_loc[1] in PREF_CITIES:
            why.append(f"★ {PREF_LABEL}")
        elif best_loc[1] == "remote":
            why.append("Remote")

    role_l = job["role"].lower()
    stack_hits = [(pts, name) for name, pts in sc["stack_match"].items() if name in role_l]
    if stack_hits:
        best = max(stack_hits)
        total += best[0]
        why.append(f"Stack match ({best[1]})")

    if "linkedin" in job.get("sources", [job.get("source", "")]) and \
            any("never mentions work authorization" in f for f in (job.get("quality_flags") or [])):
        total += sc.get("linkedin_unverified_penalty", 0)
        why.append("LinkedIn JD: no work auth mentioned (unverified)")
    if "🔥" in job["markers"] or job["company"] in faang_map:
        total += sc["faang_bonus"]
        why.append("FAANG+")
    grad_student = profile["candidate"].get("degree_level", "master") in ("master", "phd")
    if "🎓" in job["markers"] and grad_student:
        total += sc["advanced_degree_bonus"]
        why.append("Requires master's (you qualify)")
    if grad_student and re.search(r"\bmaster|graduate[\s-]level|\bgrad intern", job["role"], re.I) and \
            not re.search(r"\bundergrad", job["role"], re.I):
        total += sc.get("grad_title_bonus", 0)
        why.append("Master's only (smaller pool)")

    return min(total, 100), why


# ---------------------------------------------------------------- main pipeline

def merge(all_rows):
    """Dedupe across sources by company + term-stripped title, merging locations and sources and keeping the newest age.

    Term-aware: don't merge when both sides state terms that don't overlap --
    Tesla's Summer 2027 and Winter/Spring 2027 are different jobs.
    If one side has no term, treat them as the same (the one without is a generic title).
    """
    merged = {}
    buckets = {}                      # base_key -> [(seasons, merged_key)]
    by_ats = {}                       # ats_id -> merged_key
    for job in all_rows:
        c = re.sub(r"[^a-z0-9]+", "", job["company"].lower())
        base = f"{c}::{role_base(job['role'])}"
        js = seasons_of(job["role"])
        k = None
        # match by ATS job ID first -- more reliable than titles, catches the same job under mismatched titles
        aid = ats_id(job.get("url"))
        if aid and aid in by_ats:
            k = by_ats[aid]
        for i, (bs, bk) in enumerate(buckets.get(base, [])) if k is None else []:
            if not bs or not js or (bs & js):
                k = bk
                buckets[base][i] = (bs | js, bk)
                break
        if k is None:
            k = f"{base}::{'|'.join(sorted(js))}" if js else base
            buckets.setdefault(base, []).append((set(js), k))
        if aid:
            by_ats.setdefault(aid, k)
        if k not in merged:
            job["sources"] = [job["source"]]
            job["url_source"] = job["source"]
            job["role_source"] = job["source"]
            job["locations"] = [job["location"]] if job["location"] else []
            merged[k] = job
            continue
        m = merged[k]
        if job["source"] not in m["sources"]:
            m["sources"].append(job["source"])
        if job["location"] and job["location"] not in m["locations"]:
            m["locations"].append(job["location"])
        for mk in job["markers"]:
            if mk not in m["markers"]:
                m["markers"].append(mk)
        if job.get("age_days") is not None:
            if m.get("age_days") is None or job["age_days"] < m["age_days"]:
                m["age_days"] = job["age_days"]
        # Pick the URL by source reliability, not first-come: the off-season README is full of dead links for past terms,
        # while simplify's click links redirect to the currently valid job page
        if job["url"] and SOURCE_URL_RANK.get(job["source"], 9) < SOURCE_URL_RANK.get(
                m.get("url_source", m["source"]), 9):
            m["url"], m["url_source"] = job["url"], job["source"]
        elif not m["url"] and job["url"]:
            m["url"], m["url_source"] = job["url"], job["source"]
        if SOURCE_URL_RANK.get(job["source"], 9) < SOURCE_URL_RANK.get(
                m.get("role_source", m["source"]), 9):
            m["role"], m["role_source"] = job["role"], job["source"]
        for extra in ("salary", "sponsors_h1b", "requirements", "note",
                      "repost_note", "applicants", "quality_flags", "pay_range"):
            if job.get(extra) and not m.get(extra):
                m[extra] = job[extra]
        if job.get("force"):
            m["force"] = True
        if job.get("repost_suspect"):
            m["repost_suspect"] = True
    for k, m in merged.items():
        m["key"] = k
        m["location"] = " / ".join(m["locations"][:3]) or "—"
    return merged


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def short_loc(loc, keep=5):
    """For multi-location jobs (Google/Amazon often list 40+ cities), show only the first few, LA first."""
    parts, seen_p = [], set()
    for p in loc.split(" / "):
        p = p.strip()
        if p and p.lower() not in seen_p:
            seen_p.add(p.lower())
            parts.append(p)
    if len(parts) <= keep:
        return " / ".join(parts)
    prio = [p for p in parts if any(c in p.lower() for c in PREF_CITIES)]
    rest = [p for p in parts if p not in prio]
    shown = (prio + rest)[:keep]
    return " / ".join(shown) + f" … {len(parts)} locations"


def render_job(job, idx):
    marks = " ".join(job["markers"])
    salary = f" · 💰{job['salary']}" if job.get("salary") else ""
    why = " · ".join(job["why"][:4])
    if job.get("itar_risk"):
        why += " · ⚠️ Aerospace/defense: check if US person is required before applying"
    grad_line = ""
    if job.get("grad_note"):
        grad_line = f"   - 🎓 {job['grad_note'][:150]}\n"
    link = f"[Apply]({job['url']})" if job["url"] else "(no direct link)"
    return (f"{idx}. **{job['company']}** — {job['role']} {marks}\n"
            f"   - 📍 {short_loc(job['location'])}{salary} · Score {job['score']}\n"
            f"   - {why}\n"
            + grad_line
            + f"   - {link}\n")


def build_report(today, new_jobs, backlog, stats, faang_new, profile):
    b = profile["buckets"]
    must = [j for j in new_jobs if j["score"] >= b["must_apply"]]
    worth = [j for j in new_jobs if b["worth_applying"] <= j["score"] < b["must_apply"]]
    watch = [j for j in new_jobs if j["score"] < b["worth_applying"]]

    L = [f"# Internship report · {today}", ""]
    L.append(f"Scanned {stats['total']} raw records → {stats['merged']} jobs after dedup → "
             f"{stats['passed']} match profile → **{len(new_jobs)} new today**")
    L.append("")
    L.append(f"> Rejected: {stats['reject_summary']}")
    L.append("")

    la = [j for j in new_jobs if any(c in j["location"].lower() for c in PREF_CITIES)]
    if la:
        L.append(f"## 📍 {PREF_LABEL} ({len(la)}) — your first choice, all listed regardless of score")
        L.append("")
        L.extend([render_job(j, i) for i, j in enumerate(la, 1)])

    L.append(f"## 🔥 Must apply today ({len(must)})")
    L.append("")
    L.extend([render_job(j, i) for i, j in enumerate(must, 1)] or ["_No high-priority new jobs today._\n"])

    L.append(f"## ✅ Worth applying ({len(worth)})")
    L.append("")
    L.extend([render_job(j, i) for i, j in enumerate(worth, 1)] or ["_None._\n"])

    if watch:
        L.append(f"## 👀 Others ({len(watch)})")
        L.append("")
        for j in watch:
            marks = " ".join(j["markers"])
            link = f"[Apply]({j['url']})" if j["url"] else ""
            L.append(f"- **{j['company']}** — {j['role']} {marks} · {short_loc(j['location'], 3)} · {j['score']} pts {link}")
        L.append("")

    if backlog:
        L.append(f"## ⏳ Earlier high-score jobs not yet marked applied (Top {len(backlog)})")
        L.append("")
        for j in backlog:
            L.append(f"- **{j['company']}** — {j['role']} · {short_loc(j['location'], 3)} · "
                     f"{j['score']} pts · first seen {j['first_seen']}"
                     + (f" · [Apply]({j['url']})" if j["url"] else ""))
        L.append("")

    if faang_new:
        L.append("## 🆕 FAANG list: newly opened today")
        L.append("")
        for c, r in faang_new:
            L.append(f"- **{c}** — {r['role']}"
                     + (f" · {short_loc(clean_location(r['location']), 3)}" if r["location"] else "")
                     + (f" · 💰{r['salary']}" if r["salary"] else "")
                     + (f" · posted {r['posted']}" if r["posted"] else "")
                     + (f" · [Apply]({r['url']})" if r["url"] else ""))
        L.append("")

    L.append("---")
    L.append("After applying, run `python3 tracker.py --applied \"Company\"` to mark it; "
             "use `--skip` for ones you won't apply to, so they won't show up tomorrow.")
    return "\n".join(L)


def mark(pattern, status):
    seen = load_json(SEEN_FILE, {})
    hits = [k for k, v in seen.items()
            if pattern.lower() in f"{v['company']} {v['role']}".lower()]
    if not hits:
        print(f"No jobs matching '{pattern}'")
        return
    for k in hits:
        seen[k]["status"] = status
        print(f"  {status}: {seen[k]['company']} — {seen[k]['role']}")
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Updated {len(hits)} jobs")


def run(baseline=False):
    today = date.today()
    profile = load_profile()
    DATA.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("Fetching sources...")
    raw = fetch_all(today.isoformat())

    rows = []
    if raw["simplify"]:
        rows += parse_simplify(raw["simplify"])
    if raw["vansh"]:
        rows += parse_vansh(raw["vansh"], "vansh", today)
    if raw["vansh_offseason"]:
        rows += parse_vansh(raw["vansh_offseason"], "vansh_offseason", today)
    faang_rows = parse_faang(raw["faang"])
    # This source fails silently: truncated HTML has incomplete JSON and parse returns an empty list,
    # yet fetch_all already printed ✓. So validate explicitly here.
    if raw["faang"] and len(faang_rows) < 100:
        print(f"  ⚠️ FAANG source parsed only {len(faang_rows)} rows (normally ~1200) — "
              f"page is {len(raw['faang']):,} chars; the fetch may be truncated or upstream changed its structure. "
              f"Today's salary / H1B / graduation date requirements will be missing.")
    rows += faang_to_jobs(faang_rows, today)
    li_rows = load_linkedin_jobs(today)
    rows += li_rows
    if li_rows:
        print(f"  + LinkedIn {len(li_rows)} jobs (posted within 24h, kept in pool for 2 days)")
    manual_rows = load_manual_jobs(today)
    rows += manual_rows
    if manual_rows:
        print(f"  + Manually added {len(manual_rows)} jobs")
    # company -> list entries, used to fill in pay / H1B / graduation requirements for README jobs
    faang_map = {}
    for f in faang_rows:
        faang_map.setdefault(f["company"], []).append(f)

    merged = merge(rows)

    # Filtering
    passed, rejects = [], {}
    for job in merged.values():
        # Manual removal overrides everything, including manual adds -- removed means removed
        reason = excluded_manually(job)
        if not reason and not job.get("force"):
            reason = (reject_reason(job, profile, today)
                      or cooldown_block(job, profile, today)
                      or stale_term(job, profile))
        if reason:
            rejects[reason] = rejects.get(reason, 0) + 1
            continue
        job["score"], job["why"] = score(job, profile, faang_map)
        job["itar_risk"] = itar_risk(job, profile)
        if not job.get("salary"):
            for entry in faang_map.get(job["company"], []):
                if entry["salary"] and norm_key(entry["company"], entry["role"]) == job["key"]:
                    job["salary"] = entry["salary"]
                    break
        job["grad"], job["grad_note"] = grad_fit(job.get("requirements"), profile)
        if job["grad"] == "fit":
            job["score"] = min(job["score"] + profile["scoring"]["grad_match_bonus"], 100)
            job["why"].append("graduation date fits")
        passed.append(job)
    passed.sort(key=lambda j: -j["score"])

    # Incremental check
    seen = load_json(SEEN_FILE, {})
    new_jobs = []
    for job in passed:
        k = job["key"]
        if k not in seen:
            seen[k] = {"first_seen": today.isoformat(), "status": "new",
                       "company": job["company"], "role": job["role"],
                       "url": job["url"], "score": job["score"]}
            new_jobs.append(job)
        else:
            seen[k]["last_seen"] = today.isoformat()
            seen[k]["score"] = job["score"]
            if baseline and seen[k]["status"] in ("new", "shown"):
                new_jobs.append(job)
        if seen[k]["status"] == "new":
            seen[k]["status"] = "shown"

    # High-scoring older jobs not yet acted on (for reminders)
    passed_by_key = {j["key"]: j for j in passed}
    new_keys = {j["key"] for j in new_jobs}
    backlog = []
    for k, v in seen.items():
        if v["status"] == "shown" and k not in new_keys and k in passed_by_key:
            j = dict(passed_by_key[k], first_seen=v["first_seen"])
            if j["score"] >= profile["buckets"]["must_apply"]:
                backlog.append(j)
    backlog.sort(key=lambda j: -j["score"])
    backlog = backlog[:10]

    # FAANG snapshot diff
    # FAANG list snapshot diff: jobs closed yesterday but open today are worth applying to first
    prev_faang_path = DATA / "faang_state.json"
    prev_faang = load_json(prev_faang_path, {})
    faang_new = []
    for f in faang_rows:
        if not f["active"]:
            continue
        was = prev_faang.get(f["id"])
        if was is None or not was.get("active"):
            faang_new.append((f["company"], f))
    if faang_rows:
        prev_faang_path.write_text(json.dumps(
            {f["id"]: {"active": f["active"], "role": f["role"], "company": f["company"]}
             for f in faang_rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    faang_new = faang_new[:20]

    stats = {
        "total": len(rows), "merged": len(merged), "passed": len(passed),
        "reject_summary": ", ".join(f"{r} ({n})" for r, n in
                                    sorted(rejects.items(), key=lambda x: -x[1])[:6]),
    }
    # Write full data for dashboard.py (the report is for humans, this is for programs)
    JOBS_FILE.write_text(json.dumps([{
        "key": j["key"], "company": j["company"], "role": j["role"],
        "location": j["location"], "url": j["url"], "score": j["score"],
        "why": j["why"], "markers": j["markers"], "salary": j.get("salary", ""),
        "age_days": j.get("age_days"), "section": j.get("section", ""),
        "sources": j.get("sources", []), "itar_risk": j.get("itar_risk", False),
        "grad": j.get("grad"), "grad_note": j.get("grad_note", ""),
        "force": j.get("force", False), "note": j.get("note", ""),
        "repost_suspect": j.get("repost_suspect", False),
        "repost_note": j.get("repost_note", ""),
        "applicants": j.get("applicants"),
        "quality_flags": j.get("quality_flags") or [],
        "pay_range": j.get("pay_range", ""),
        "status": seen.get(j["key"], {}).get("status", "shown"),
        "first_seen": seen.get(j["key"], {}).get("first_seen", ""),
        "is_la": any(c in j["location"].lower() for c in PREF_CITIES),
    } for j in passed], ensure_ascii=False, indent=1), encoding="utf-8")

    new_jobs.sort(key=lambda j: -j["score"])
    report = build_report(today.isoformat(), new_jobs, backlog, stats, faang_new, profile)
    out = REPORTS / f"{today.isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{stats['total']} rows → deduped {stats['merged']} → passed {stats['passed']} → new {len(new_jobs)}")
    print(f"Daily report: {out}")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", action="store_true", help="First full run")
    p.add_argument("--applied", metavar="KEYWORD", help="Mark as applied")
    p.add_argument("--skip", metavar="KEYWORD", help="Mark as skipped")
    a = p.parse_args()
    if a.applied:
        mark(a.applied, "applied")
    elif a.skip:
        mark(a.skip, "skipped")
    else:
        run(baseline=a.baseline)
