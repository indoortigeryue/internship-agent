#!/usr/bin/env python3
"""Fetch the real JD page for each pending job to verify it, dropping dead links and roles that explicitly refuse sponsorship.

Why this step is required: the two GitHub READMEs **only list job titles, not full JDs**,
while sponsorship restrictions are often stated only in the JD body (Walmart's says
"IMMIGRATION SPONSORSHIP IS NOT AVAILABLE FOR THIS ROLE.",
and the README maintainer didn't add the 🛂 marker, so marker-only filtering will always miss some).

Results are cached in data/verified.json keyed by URL, and verified jobs aren't refetched --
so only new jobs hit the network each day instead of hundreds of requests per run.

Usage:
    python3 verify.py              # dry run, print only
    python3 verify.py --write      # write verdicts back to seen.json
    python3 verify.py --recheck    # ignore cache, refetch everything
"""

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import fetchlib

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "data" / "jobs.json"
SEEN = ROOT / "data" / "seen.json"
CACHE = ROOT / "data" / "verified.json"
PROFILE = ROOT / "profile.json"

# Which restrictions apply depends on the user's work authorization (set by setup.py):
# sponsorship statements only matter if they need sponsorship; "US person" export rules
# don't exclude green-card holders; citizens pass every eligibility check.
_P = json.loads(PROFILE.read_text(encoding="utf-8")) if PROFILE.exists() else {}
_C = _P.get("candidate", {})
AUTH = _C.get("work_authorization", "f1")
NEEDS_SPONSOR = _C.get("needs_sponsorship", True)
US_CITIZEN = AUTH == "us_citizen"
US_PERSON = AUTH in ("us_citizen", "permanent_resident")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Phrasings that explicitly say no sponsorship. All are full phrases, to avoid matching
# opposite sentences like "we sponsor visas" / "sponsorship available".
NO_SPONSOR = [
    r"sponsorship is not available",
    r"immigration sponsorship is not available",
    # Qualifiers often sit between the verb and "visa" -- Chevron's text is
    # "unable to sponsor **employment** Visas"; an earlier \s*-adjacent pattern missed it entirely,
    # so the job was judged ok (found 2026-09-30). Allow 0-2 words of slack.
    r"(?:are |is )?not (?:able|willing) to (?:provide|offer|sponsor)\w*(?:\s+\w+){0,2}\s+(?:visa|immigration|sponsorship)",
    r"unable to (?:provide|offer|sponsor)\w*(?:\s+\w+){0,2}\s+(?:visa|immigration|sponsorship)",
    r"(?:do(?:es)? not|will not|cannot|can't) (?:provide|offer|sponsor)\w*(?:\s+\w+){0,2}\s+(?:visa|immigration|sponsorship|work authorization)",
    r"no visa sponsorship",
    r"without (?:the need for |any )?(?:visa |immigration )?sponsorship",
    r"not (?:eligible|available) for (?:visa |immigration )?sponsorship",
    r"does not sponsor",
    r"we do not sponsor",
    r"authorized to work in the (?:u\.?s\.?|united states)[^.]{0,60}without (?:company |visa |current or future )?sponsorship",
    # ↓ added 2026-09-28. About 40-50% of listings turned out to refuse sponsorship only after opening them,
    #   because the phrasings below **never contain the word "sponsorship"**, or use a different word order.
    r"must not require (?:visa |immigration )?sponsorship",
    r"(?:will|do|does) not (?:now or in the future )?require (?:visa |immigration )?sponsorship",
    r"not (?:now or in the future )?require (?:visa |immigration )?sponsorship (?:for|to)",
    r"unrestricted (?:right|authorization|authorisation) to work",
    r"authoriz\w+ to work[^.]{0,50}without restriction",
    r"work authorization[^.]{0,40}without restriction",
    r"(?:currently )?on cpt/?opt[^.]{0,40}(?:are )?ineligible",
    r"cpt\s*/?\s*opt[^.]{0,30}not (?:be )?(?:eligible|accepted)",
]

# Export control / US person. Added 2026-09-28: these are excluded too.
# **Must rely on the CONDITIONAL guard** -- bare "export control" is boilerplate
# disclaimer text in many JDs (Datadog's "some of these roles may require"), so on its own it causes false removals.
EXPORT_CONTROL = [
    r"must be (?:a |an )?u\.?s\.? person",
    r"\bu\.?s\.? person\b[^.]{0,60}(?:required|due to|because)",
    r"due to[^.]{0,60}export[- ]control",
    r"subject to[^.]{0,60}export control",
    r"must comply with[^.]{0,40}export control",
    r"\bitar\b[^.]{0,40}(?:compliance|required|restrict)",
    r"export[- ]controlled (?:information|data|technology|facilities)",
]

# Match only **hard requirements**. Bare "export control" / "security clearance" don't count --
# many US company JDs carry this boilerplate disclaimer (Datadog does), so treating it as a hard requirement causes false removals.
CITIZEN_ONLY = [
    r"must be a (?:u\.?s\.?|united states) citizen",
    r"must be (?:a )?(?:u\.?s\.?|united states) person",
    r"(?:u\.?s\.?|united states) citizenship (?:is )?required",
    r"require[sd]? (?:u\.?s\.?|united states) citizenship",
    r"requires? (?:an )?active [^.]{0,30}clearance",
    r"must (?:currently )?(?:hold|possess) (?:an? )?active [^.]{0,30}clearance",
    r"must be able to obtain (?:and maintain )?(?:a )?(?:u\.?s\.? )?security clearance",
    r"\bts/sci\b",
]

# When "no sponsorship" is scoped to **full-time** only, the internship itself is unaffected -- an F-1 student
# interns on CPT and only misses a full-time return offer. Mary Kay's posting is like this; it used to need
# manual approval via manual_verdicts.json, now the rule recognizes it.
# Must exclude "full-time internship", which refers to the internship itself.
FULLTIME_SCOPED = re.compile(
    r"full[- ]time (?:employment|positions?|roles?|hires?|offers?|opportunities)|"
    r"permanent (?:employment|positions?|roles?)", re.I)
FULLTIME_INTERN = re.compile(r"full[- ]time intern", re.I)


# When these qualifiers appear in the JD, the phrasings above are only "possible", not hard requirements
CONDITIONAL = re.compile(
    r"may (?:require|be required)|some (?:of these )?(?:roles|positions)|"
    r"depending on|if applicable|certain (?:roles|positions)|"
    r"this position may|could require|where applicable",
    re.I)

# "...accepts applications on an ongoing basis until the position is filled" means it's **still open**,
# but it would match "position is filled" below. Dead-link detection needs its own guard --
# it can't share CONDITIONAL, because Michelin's JD has "on an ongoing, indefinite basis",
# which is a genuine no-sponsorship statement; a shared guard would let it through too.
DEAD_GUARD = re.compile(
    r"until (?:the )?position|until filled|remains? open|"
    r"accept\w* applications on an ongoing basis|ongoing basis until|"
    r"posting will remain",
    re.I)

DEAD_PAGE = [
    r"job you'?re looking for isn'?t available",
    r"no longer (?:available|accepting)",
    r"position (?:has been|is) (?:filled|closed)",
    r"this (?:job|position|requisition) (?:is )?(?:no longer|has been) ",
    r"job not found",
    r"posting (?:has )?expired",
    r"we couldn'?t find that job",
    r"requisition (?:is )?closed",
]


# Oracle Recruiting Cloud job pages are JS-rendered, so fetching HTML only gets an empty shell,
# but a public REST API returns the full JD (Tradeweb and 16 other jobs are hosted there)
ORACLE_RE = re.compile(
    r"https?://([^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^?]*?/job/(\d+)", re.I)


GREENHOUSE_RE = re.compile(
    r"(?:job-)?boards\.greenhouse\.io/(?!embed)([a-z0-9_-]+)/jobs/(\d+)", re.I)
ASHBY_RE = re.compile(
    r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)/([0-9a-f-]{36})", re.I)

# Many companies use Greenhouse as the backend behind their own frontend (careerpuck, careers.withwaymo.com,
# etc.); the page is an empty JS shell, but the gh_jid in the URL is the Greenhouse job id.
# Paired with a board token, the official API returns the full JD. The token isn't in the URL and must be guessed,
# so greenhouse_targets() returns a list of candidates to try in order.
GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)", re.I)
CAREERPUCK_RE = re.compile(r"careerpuck\.com/job-board/([a-z0-9_-]+)/job/(\d+)", re.I)

# Workday job pages are JS-rendered too, but every tenant has a public cxs JSON endpoint:
#   https://<tenant>.wd3.myworkdayjobs.com/<site>/job/<path>
#     -> https://<tenant>.wd3.myworkdayjobs.com/wday/cxs/<tenant>/<site>/job/<path>
# Some tenants lock this endpoint (philips returns 403 permission denied); if locked, fall back to the page.
WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/job/([^?#]+)", re.I)

# Workday has a second domain form: the tenant is in the path, /recruiting/<tenant>/<site>/, not the subdomain.
# Snap uses this (wd1.myworkdaysite.com/recruiting/snapchat/snap/job/...).
# The transform replaces /recruiting/ with /wday/cxs/. The public page yields just 1 character of plain text,
# so without the API it's always judged unknown.
WORKDAY_SITE_RE = re.compile(
    r"(https?://wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"recruiting/([^/?#]+)/([^/?#]+)/job/([^?#]+)", re.I)

# Workable and BambooHR are likewise JS-rendered with public APIs. Their APIs also expose
# job status (Workable's state, BambooHR's jobOpeningStatus), far more reliable than regex-guessing
# "position has been filled", so dead links are judged from status directly.
WORKABLE_RE = re.compile(r"apply\.workable\.com/([a-z0-9_-]+)/j/([a-z0-9]+)", re.I)
BAMBOO_RE = re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com/careers/(\d+)", re.I)

# simplify.jobs /jobs/click/<uuid> is a wrapper redirect with no content of its own;
# it must be resolved first to see which ATS is behind it.
WRAPPER_RE = re.compile(r"^https?://(?:www\.)?simplify\.jobs/jobs?/click/", re.I)


def greenhouse_targets(url, company=""):
    """Return [(board_token, job_id), ...], ordered by confidence, at most 3.

    How tokens are guessed (in practice careerpuck slugs don't always match Greenhouse tokens,
    e.g. domino-data-lab on careerpuck vs dominodatalab on Greenhouse):
      - careerpuck: the slug in the URL, plus a version with hyphens removed
      - other custom frontends: the domain core with prefixes/suffixes like careers./jobs./with stripped
        (careers.withwaymo.com -> withwaymo -> waymo)
      - fallback: the company name from the job record, non-alphanumerics removed
    """
    m = GREENHOUSE_RE.search(url)
    if m:                                   # already the official Greenhouse domain, no guessing needed
        return [(m.group(1).lower(), m.group(2))]

    cp, jid = CAREERPUCK_RE.search(url), GH_JID_RE.search(url)
    if not cp and not jid:
        return []
    job_id = jid.group(1) if jid else cp.group(2)

    cands = []
    if cp:
        slug = cp.group(1).lower()
        cands += [slug, slug.replace("-", "")]
    else:
        core = re.sub(r"^(?:www|careers?|jobs?|apply|boards?|my)\.", "",
                      urllib.parse.urlparse(url).netloc.lower()).split(".")[0]
        cands += [core, re.sub(r"^(?:with|join|life-?at|team)", "", core)]
    cands.append(re.sub(r"[^a-z0-9]", "", company.lower()))

    out, seen_tok = [], set()
    for c in cands:
        if c and c not in seen_tok:
            seen_tok.add(c)
            out.append((c, job_id))
    return out[:3]


def workday_api(url):
    m = WORKDAY_RE.search(url)
    if m:
        tenant, wd, site, path = m.groups()
        return (f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/"
                f"{tenant}/{site}/job/{path}")
    m = WORKDAY_SITE_RE.search(url)
    if m:
        host, tenant, site, path = m.groups()
        return f"{host}/wday/cxs/{tenant}/{site}/job/{path}"
    return None


def fetch_jd(url, company=""):
    """Get the full JD text. Several major ATS pages are JS-rendered, but all have public APIs:

    - Oracle Recruiting Cloud: hcmRestApi, returns ExternalDescriptionStr
    - Greenhouse: boards-api.greenhouse.io/v1/boards/<org>/jobs/<id>, returns content
      (including reskinned frontends like careerpuck / withwaymo, detected via gh_jid in the URL)
    - Workday: /wday/cxs/<tenant>/<site>/job/<path>, returns jobPostingInfo
    - Ashby: api.ashbyhq.com/posting-api/job-board/<org>, returns the whole board; look up by id

    Returns (status, raw, title, meta). raw is the raw text for the regexes;
    meta holds location / salary when the API provides them directly.
    """
    # Resolve wrapper redirects first, otherwise every ATS match below fails
    if WRAPPER_RE.match(url):
        url = fetchlib.final_url(url)

    # Oracle
    api = oracle_api(url)
    if api:
        st, raw, _ = fetch(api)
        if raw and not raw.startswith("__ERROR__") and len(raw) > 200:
            return st, raw, real_title(raw), {}

    # Greenhouse (official domain matches directly; reskinned frontends try several board tokens)
    for token, job_id in greenhouse_targets(url, company):
        st, raw, _ = fetch(f"https://boards-api.greenhouse.io/v1/boards/"
                           f"{token}/jobs/{job_id}")
        if raw and not raw.startswith("__ERROR__") and len(raw) > 200:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("title"):          # a wrong token makes the API return error JSON
                return st, raw, d["title"], {
                    "location": (d.get("location") or {}).get("name", "")}

    # Workday
    api = workday_api(url)
    if api:
        st, raw, _ = fetch(api)
        if raw and not raw.startswith("__ERROR__") and len(raw) > 200:
            try:
                d = json.loads(raw).get("jobPostingInfo") or {}
            except ValueError:
                d = {}
            if d.get("jobDescription"):
                return st, raw, d.get("title", ""), {
                    "location": d.get("location", "")}

    # Workable
    m = WORKABLE_RE.search(url)
    if m:
        st, raw, _ = fetch(f"https://apply.workable.com/api/v1/accounts/"
                           f"{m.group(1)}/jobs/{m.group(2)}")
        if raw and not raw.startswith("__ERROR__") and len(raw) > 200:
            try:
                d = json.loads(raw)
            except ValueError:
                d = {}
            if d.get("title"):
                loc = d.get("location") or {}
                return st, raw, d["title"], {
                    "location": ", ".join(x for x in (loc.get("city"), loc.get("region"))
                                          if x),
                    # state other than published means withdrawn/unpublished
                    "gone": d.get("state") not in (None, "published")}

    # BambooHR
    m = BAMBOO_RE.search(url)
    if m:
        st, raw, _ = fetch(f"https://{m.group(1)}.bamboohr.com/careers/{m.group(2)}/detail")
        if raw and not raw.startswith("__ERROR__") and len(raw) > 200:
            try:
                d = (json.loads(raw).get("result") or {}).get("jobOpening") or {}
            except ValueError:
                d = {}
            if d.get("jobOpeningName"):
                return st, raw, d["jobOpeningName"], {
                    "location": str(d.get("location") or ""),
                    "gone": str(d.get("jobOpeningStatus", "Open")).lower() != "open"}

    # Ashby: the API returns the whole board; look up by posting id.
    # If the board loads fine but lacks this id, the posting has been withdrawn.
    # The board API is the official list of open roles, more reliable than regex-scanning page text.
    m = ASHBY_RE.search(url)
    if m:
        st, raw, _ = fetch(f"https://api.ashbyhq.com/posting-api/job-board/"
                           f"{m.group(1)}?includeCompensation=true")
        if raw and not raw.startswith("__ERROR__"):
            try:
                posts = json.loads(raw).get("jobs", [])
            except ValueError:
                posts = None
            if posts is not None:
                for post in posts:
                    if post.get("id", "").lower() == m.group(2).lower():
                        return (st, json.dumps(post, ensure_ascii=False),
                                post.get("title", ""),
                                {"location": post.get("location", "")})
                if posts:
                    return st, raw, "", {"gone": True,
                                         "gone_note": f"this id is not among the {len(posts)} open jobs on the Ashby board; the posting was taken down"}

    # Nothing matched: fetch the page directly
    st, raw, _ = fetch(url)
    return st, raw, (real_title(raw) if raw and not raw.startswith("__ERROR__") else ""), {}


def oracle_api(url):
    m = ORACLE_RE.search(url)
    if not m:
        return None
    host, job_id = m.group(1), m.group(2)
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f'?expand=all&onlyData=true&finder=ById;Id="{job_id}",siteNumber=CX')


# Direction mismatch: pure C++ / hardware / AI-ML roles are out of scope
MY_LANGS = ["python", "java", "javascript", "typescript", "sql", "node", "react",
            "web ", "backend", "back-end", "full stack", "full-stack", "api", "cloud"]
ML_WORDS = ["pytorch", "tensorflow", "model training", "transformer", "neural network",
            "deep learning", "machine learning", "fine-tun", "computer vision",
            "reinforcement learning", "embedding"]
HARDWARE_HINTS = [
    r"hardware[- ]in[- ]the[- ]loop", r"\bhil\b", r"\bsil\b", r"\bfirmware\b",
    r"\bembedded\b", r"\bpcb\b", r"\bverilog\b", r"\bfpga\b", r"oscilloscope",
    r"soldering", r"\bbring-?up\b",
]


# Trade-off for direction checks: **a false removal is far worse than a false flag** -- a false flag costs
# the user 10 seconds to skip; a false removal means they never learn what they missed. Classifying by JD body misfired both ways:
#   - "Knowledge of modern software languages – C, C++, Java, Python"
#     was judged a pure C++ role (a Tradeweb Java role, actually the best fit)
#   - Kensho's "With expertise in machine learning…, we develop" is the company blurb;
#     the actual duties are "write server-side applications / design APIs / distributed computing",
#     a backend platform role at an AI company, a great fit
# So only two high-confidence signals cause removal; everything else is just flagged.

# Only look at direction words in the job title. **Can't use a bare \bai\b** -- many company names contain AI
# (Shield AI, Scale AI), which would wrongly remove normal SWE roles.
TITLE_BAD = re.compile(
    r"\bai\s*/\s*ml\b|machine learning|deep learning|\baml\b|\bllm\b|gen\s*ai|"
    r"computer vision|\bnlp\b|c\+\+|\bembedded\b|\bfirmware\b|\bhardware\b|"
    r"\bfpga\b|\bsilicon\b|\bvlsi\b|\bcompiler\b|\basic\b", re.I)
# setup.py narrows this to the role families the user did NOT pick (empty list = no title check)
if "title_mismatch_regex" in _P.get("hard_exclude", {}):
    _tm = _P["hard_exclude"]["title_mismatch_regex"]
    TITLE_BAD = re.compile("|".join(_tm), re.I) if _tm else re.compile(r"(?!x)x")

# Languages/areas the candidate knows. If the JD mentions any, it's not a "pure C++ role"
MY_LANGS = ["python", "java", "javascript", "typescript", "sql", "node", "react",
            "backend", "back-end", "full stack", "full-stack", "web ", "\bapi\b",
            "cloud", "django", "flask", "spring"]
MY_LANGS += [re.escape(x.lower()) for x in _C.get("stack", []) if x.lower() != "c++"]

SOFT_HINTS = {
    "ML focus": [r"machine learning|deep learning|pytorch|tensorflow|computer vision|\bllm\b"],
    "C++": [r"\bc\+\+"],
    "firmware/embedded/silicon": [r"hardware[- ]in[- ]the[- ]loop|\bhil\b|\bfirmware\b|\bembedded\b|"
                  r"custom silicon|device driver|\bpcb\b|\bverilog\b"],
}


def direction(text, title=""):
    """Return (verdict, evidence). Only two verdicts cause removal:

    - `title_mismatch`: the job title itself is AI/ML/C++/hardware
    - `cpp_only`: the JD mentions C++ and **none** of the candidate's languages/areas

    Everything else returns `soft_*`: the job is kept and only flagged on the dashboard.
    """
    if title and TITLE_BAD.search(title):
        return "title_mismatch", f"job title: {title}"
    low = text.lower()
    if re.search(r"\bc\+\+", low) and not any(re.search(k, low) for k in MY_LANGS):
        return "cpp_only", find_first(text, [r"[^.;]{0,120}\bc\+\+[^.;]{0,80}"], guards=())
    for tag, pats in SOFT_HINTS.items():
        ev = find_first(text, pats, guards=())
        if ev:
            return f"soft_{tag}", ev
    return None, None


def real_title(raw):
    for pat in (r'"Title"\s*:\s*"((?:[^"\\]|\\.)*)"',
                r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"',
                r'<h1[^>]*>(.*?)</h1>',
                r'property="og:title"\s+content="([^"]+)"'):
        m = re.search(pat, raw, re.S | re.I)
        if m:
            t = m.group(1)
            # Titles are often pulled from JSON embedded in the page, containing JSON escapes like \u0026
            if "\\u" in t or "\\/" in t:
                try:
                    t = json.loads(f'"{t}"')
                except ValueError:
                    pass
            t = html.unescape(re.sub(r"<[^>]+>", " ", t))
            t = re.sub(r"\s+", " ", t).strip()
            if 3 < len(t) < 160:
                return t
    return ""


def fetch(url, timeout=25):
    """Fetching is delegated to fetchlib -- on this machine urllib fails the handshake with some sites
    because LibreSSL 2.8.3 lacks TLS 1.3; fetchlib automatically falls back to curl."""
    status, text = fetchlib.http_get(url, timeout=timeout)
    if status is None and not text:
        return None, "__ERROR__fetch failed (neither urllib nor curl got the page)", url
    return status, text, url


def page_text(raw):
    """Get the page's plain text. Workday / Greenhouse and others put the JD in the JSON-LD
    description field, which is cleaner than stripped HTML, so prefer it.
    (Workday's cxs endpoint uses the key jobDescription, so match that too.)"""
    chunks = []
    for m in re.finditer(r'"(?:description|jobDescription)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        try:
            d = json.loads(f'"{m.group(1)}"')
        except ValueError:
            continue
        # Workday and others put **double-escaped** HTML into JSON: after json decoding it's still
        # `&lt;h2&gt;Job Title:&lt;/h2&gt;`. Unescaping only once leaves the tags in place,
        # and evidence snippets render as a pile of &lt;p&gt; (as happened with Activision).
        d = html.unescape(d)
        d = re.sub(r"<[^>]+>", " ", d)
        chunks.append(html.unescape(d))
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    chunks.append(html.unescape(body))
    return re.sub(r"\s+", " ", " ".join(chunks))


def find_first(text, patterns, guards=(CONDITIONAL,)):
    """A match still needs context: sentences with qualifiers like 'may require' / 'until the position is filled'
    are boilerplate or opposite statements and don't count."""
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            s = max(0, m.start() - 220)
            window = re.sub(r"\s+", " ", text[s:m.end() + 220]).strip()
            if any(g.search(window) for g in guards):
                continue
            # Sponsorship statements scoped to full-time don't apply to internships
            if FULLTIME_SCOPED.search(window) and not FULLTIME_INTERN.search(window):
                continue
            return window
    return None


# "regex" (default, zero-dependency) or "cascade": hand the eligibility decision
# to the screener's LLM agents (../screener). Set by --engine.
ENGINE = "regex"
SCREENER = ROOT.parent / "screener"


def agent_eligibility(text, company, title):
    """Eligibility via the screener cascade: Haiku triage, Opus only when uncertain.
    Returns (verdict, evidence quote, $ cost)."""
    if str(SCREENER) not in sys.path:
        sys.path.insert(0, str(SCREENER))
    import agents  # needs the anthropic SDK: run with the repo's .venv python
    job = {"id": "", "company": company, "title": title, "role": title, "text": agents.dedupe(text)}
    r = agents.cascade(job, agents.HAIKU, agents.OPUS)
    e = r["extraction"]
    quote = e["sponsorship_quote"] if r["verdict"] == "no_sponsorship" else e["citizenship_quote"]
    return r["verdict"], quote, r["cost"]


def check(url, company="", stored_jd=""):
    status, raw, api_title, meta = fetch_jd(url, company)
    # LinkedIn blocks most automated page fetches (403/429), but linkedin.py already saved
    # the description when it crawled. Judge that instead of giving up as "blocked".
    if len(stored_jd) >= 400 and (raw.startswith("__ERROR__") or status in (401, 403, 429)
                                  or status is None or len(page_text(raw)) < 400):
        return {**judge(stored_jd, api_title or "", status, company), "source": "stored LinkedIn JD"}
    if raw.startswith("__ERROR__"):
        return {"verdict": "unknown", "note": raw[9:][:120], "http": None}
    # The ATS API says outright the job isn't open -- more reliable than scanning page text, so trust it first
    if meta.get("gone"):
        return {"verdict": "dead", "http": status,
                "note": meta.get("gone_note", "ATS API says the job status is not 'open'"),
                "real_title": api_title}
    # 403/429 is anti-bot, not a missing job; only 404/410 count as dead links
    if status in (403, 429, 401):
        return {"verdict": "blocked", "note": f"HTTP {status} anti-bot, open manually to check", "http": status}
    if status is None or status >= 400:
        return {"verdict": "dead", "note": f"HTTP {status}", "http": status}

    text = page_text(raw)
    if len(text) < 400:
        return {"verdict": "unknown", "note": "too little page content, may need JS rendering", "http": status}

    ev = find_first(text, DEAD_PAGE, guards=(CONDITIONAL, DEAD_GUARD))
    if ev:
        return {"verdict": "dead", "note": ev, "http": status}
    return judge(text, api_title or real_title(raw), status, company)


def judge(text, title, status, company):
    """Eligibility + direction verdict for JD text (fetched page or stored LinkedIn JD)."""
    if US_CITIZEN:
        pass  # no sponsorship / citizenship / export rule can exclude a US citizen
    elif ENGINE == "cascade":
        v, quote, cost = agent_eligibility(text, company, title)
        if v != "eligible":
            return {"verdict": v, "note": quote, "http": status, "engine": "cascade", "cost": cost}
    else:
        ev = find_first(text, NO_SPONSOR) if NEEDS_SPONSOR else None
        if ev:
            return {"verdict": "no_sponsorship", "note": ev, "http": status}
        ev = find_first(text, CITIZEN_ONLY)
        if ev:
            return {"verdict": "citizen_only", "note": ev, "http": status}
        ev = find_first(text, EXPORT_CONTROL) if not US_PERSON else None
        if ev:
            return {"verdict": "citizen_only", "note": ev, "http": status}

    d, dev = direction(text, title)
    if d in ("cpp_only", "title_mismatch"):
        return {"verdict": d, "note": dev or "", "http": status, "real_title": title}
    return {"verdict": "ok", "note": "", "http": status,
            "real_title": title, "flag": d or "", "flag_note": dev or ""}


REASON = {
    "dead": "dead link",
    "no_sponsorship": "JD explicitly says no visa sponsorship",
    "citizen_only": "JD requires US citizenship / clearance",
    "cpp_only": "JD only mentions C++, none of the candidate's languages",
    "title_mismatch": "job title itself is AI/ML/C++/hardware",
}


def main(write, recheck, limit):
    jobs = json.loads(JOBS.read_text(encoding="utf-8"))
    seen = json.loads(SEEN.read_text(encoding="utf-8"))
    cache = {} if recheck else (json.loads(CACHE.read_text(encoding="utf-8"))
                                if CACHE.exists() else {})

    # JD text linkedin.py saved while crawling: lets LinkedIn postings be judged even when
    # the page itself is blocked, and lets earlier "blocked" verdicts be retried with it.
    li = ROOT / "data" / "linkedin_jobs.json"
    stored = {r["url"]: r["jd"] for r in (json.loads(li.read_text(encoding="utf-8")).get("jobs", [])
                                          if li.exists() else []) if len(r.get("jd") or "") >= 400}
    todo = [j for j in jobs if j["status"] == "shown" and j["url"] and (
        j["url"] not in cache
        or (j["url"] in stored and cache[j["url"]].get("verdict") in ("blocked", "unknown")
            and not cache[j["url"]].get("manual")))]
    todo.sort(key=lambda j: -j["score"])
    if limit:
        todo = todo[:limit]

    print(f"To apply: {sum(1 for j in jobs if j['status'] == 'shown')}; "
          f"{len(todo)} of them need checking (the rest are cached)\n")

    for i, j in enumerate(todo, 1):
        res = check(j["url"], j.get("company", ""), stored.get(j["url"], ""))
        res["checked"] = date.today().isoformat()
        res["engine"] = "regex" if US_CITIZEN else ENGINE
        res["company"], res["role"] = j["company"], j["role"]
        cache[j["url"]] = res
        icon = "✓" if res["verdict"] == "ok" else ("?" if res["verdict"] in
               ("unknown", "blocked") else "✗")
        line = f"  [{i}/{len(todo)}] {icon} {j['company']} — {j['role'][:40]}"
        rt = res.get("real_title", "")
        if rt and rt.lower() not in j["role"].lower():
            line += f"\n        real title: {rt}"      # simplify normalizes titles
        if res["verdict"] != "ok":
            line += f"\n        {res['verdict']}: {res['note'][:150]}"
        elif res.get("flag"):
            line += f"\n        ⚑ {res['flag'].replace('soft_','')}: {res['flag_note'][:120]}"
        print(line, flush=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    bad = [(j, cache[j["url"]]) for j in jobs
           if j["status"] == "shown" and j["url"] in cache
           and cache[j["url"]]["verdict"] in REASON]
    print(f"\n=== {len(bad)} to drop ===")
    for j, r in sorted(bad, key=lambda x: -x[0]["score"]):
        print(f"  {j['score']:>3}pt {j['company']} — {j['role'][:45]}  [{REASON[r['verdict']]}]")

    if write:
        for j, r in bad:
            if j["key"] in seen:
                seen[j["key"]]["status"] = "skipped"
                seen[j["key"]]["skip_reason"] = REASON[r["verdict"]]
                seen[j["key"]]["skip_evidence"] = r["note"][:300]
        SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nWrote back to seen.json, {len(bad)} marked as skipped")
    else:
        print("\n(dry run. Add --write to apply)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--engine", choices=("regex", "cascade"), default="regex",
                    help="cascade = LLM agents from ../screener (needs ANTHROPIC_API_KEY)")
    a = ap.parse_args()
    ENGINE = a.engine
    main(a.write, a.recheck, a.limit)
