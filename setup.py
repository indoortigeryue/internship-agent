#!/usr/bin/env python3
"""First-run setup: asks who you are and what you're looking for, then writes
tracker/profile.json, which drives every filter, score, and eligibility check.

    python3 setup.py

Steps: 1) about you (status, degree, graduation; optionally read from your
resume by Claude), 2) what you're looking for (roles, skills, locations,
terms), 3) review, then write. Re-run any time to change answers; the old
profile is kept as profile.json.bak.

Your answers stay on this machine: profile.json is gitignored. The resume is
only sent anywhere if you choose AI parsing (to the Anthropic API).
"""

import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRACKER = ROOT / "tracker"
EXAMPLE = TRACKER / "profile.example.json"
OUT = TRACKER / "profile.json"
ENV = ROOT / ".env"  # API key lives here (gitignored, mode 600), never in profile.json
sys.path.insert(0, str(TRACKER))

AUTH = {
    "us_citizen": "US citizen",
    "permanent_resident": "US permanent resident (green card)",
    "f1": "F-1 student (CPT / OPT)",
    "h1b": "H-1B holder (needs a transfer)",
    "other_sponsorship": "Other, needs visa sponsorship",
}
DEGREE = {"bachelor": "Bachelor's", "master": "Master's", "phd": "PhD"}

# Role families. The general SWE families only add scoring signals; the "scope"
# families are excluded by default and re-enabled when picked.
FAMILIES = {
    "backend": {"label": "Backend / distributed systems",
                "score": {"backend": 16, "back-end": 16, "distributed": 16, "api": 12, "database": 12}},
    "fullstack": {"label": "Full-stack", "score": {"full stack": 12, "full-stack": 12, "fullstack": 12}},
    "frontend": {"label": "Frontend / web", "score": {"frontend": 14, "front-end": 14, "web": 10, "react": 10}},
    "infra": {"label": "Infrastructure / cloud / SRE",
              "score": {"infrastructure": 14, "platform": 14, "cloud": 14, "site reliability": 10, "devops": 10}},
    "mobile": {"label": "Mobile (iOS / Android)", "score": {"mobile": 14, "ios": 14, "android": 14}},
    "ml": {"label": "AI / ML", "scope": True,
           "include": ["machine learning", "ml engineer", "ai engineer", "ai/ml", "ai / ml"],
           "keywords": ["ai/ml", "ai / ml", "ml engineer", "machine learning", "deep learning",
                        "artificial intelligence", "computer vision", "nlp", "llm", "generative ai",
                        "genai", "research engineer"],
           "regex": [r"\bai\b", r"\bml\b", r"\bai\s*/\s*ml\b", "machine learning", "deep learning",
                     r"\bllm\b", r"gen\s*ai", "computer vision", r"\bnlp\b", r"\brag\b"],
           "title": 0, "jd": "ml_heavy", "score": {"machine learning": 14, "ml": 12, "ai": 10}},
    "data": {"label": "Data engineering / analytics", "scope": True,
             "include": ["data engineer", "data engineering", "analytics engineer", "data platform"],
             "keywords": ["data scientist"],
             "regex": [r"\bdata\s+(?:engineer|engineering|analyst|analytics|scien|architect|warehouse)",
                       r"\bdata\s*(?:&|and)\s*(?:analytics|digital|ai)\b", r"\banalytics\s+engineer",
                       r"\bdatabase\s+engineer", r"\bbusiness intelligence\b",
                       r"\bbi\s+(?:engineer|developer|analyst)\b", r"\betl\b", r"\bmarket data\b"],
             "score": {"data": 12}},
    "hardware": {"label": "Embedded / firmware / hardware", "scope": True,
                 "include": ["embedded", "firmware", "hardware engineer"],
                 "keywords": ["hardware engineer", "electrical engineer", "asic", "rtl", "vlsi", "fpga",
                              "analog", "silicon", "circuit", "pcb", "embedded", "firmware",
                              "device driver", "kernel", "robotics"],
                 "regex": [r"\bhardware\b", r"\bfirmware\b", r"\bembedded\b", r"\bfpga\b", r"\basic\b",
                           r"\bsilicon\b", r"\bvlsi\b"],
                 "title": 2, "score": {"embedded": 14, "firmware": 12}},
    "systems": {"label": "C++ / systems / graphics / compilers", "scope": True,
                "include": ["c++", "graphics", "compiler"],
                "keywords": ["gpu", "cuda", "compiler", "graphics", "game engine"],
                "regex": [r"c\+\+"], "title": 1, "jd": "cpp_only",
                "score": {"c++": 12, "systems": 10, "graphics": 12}},
    "qa": {"label": "QA / test engineering", "scope": True,
           "include": ["qa", "test engineer", "sdet"],
           "regex": [r"\bqa\b", r"\bsdet\b", "quality assurance", r"\btest engineer", r"\btest engineering",
                     r"\bsoftware test\b"], "score": {"test": 8}},
}
DEFAULT_FAMILIES = ["backend", "fullstack", "infra"]

METROS = {
    "la": ("Los Angeles", None),  # filled from tracker.DEFAULT_LA
    "sf": ("SF Bay Area", ["san francisco", "south san francisco", "oakland", "berkeley", "emeryville",
                           "san mateo", "burlingame", "redwood city", "menlo park", "palo alto",
                           "mountain view", "sunnyvale", "santa clara", "san jose", "cupertino",
                           "milpitas", "fremont", "foster city", "san bruno", "los gatos", "pleasanton"]),
    "nyc": ("New York", ["new york", "nyc", "brooklyn", "manhattan", "jersey city", "hoboken",
                         "long island city", "queens"]),
    "seattle": ("Seattle", ["seattle", "bellevue", "redmond", "kirkland", "bothell", "tacoma"]),
    "boston": ("Boston", ["boston", "cambridge", "somerville", "waltham", "burlington", "lexington"]),
    "austin": ("Austin", ["austin", "round rock"]),
    "chicago": ("Chicago", ["chicago", "evanston"]),
    "sandiego": ("San Diego", ["san diego", "la jolla", "carlsbad"]),
    "dc": ("Washington DC", ["washington, dc", "washington dc", "arlington", "reston", "mclean",
                             "herndon", "tysons", "bethesda", "alexandria"]),
    "denver": ("Denver / Boulder", ["denver", "boulder"]),
    "atlanta": ("Atlanta", ["atlanta"]),
    "dallas": ("Dallas", ["dallas", "plano", "irving", "fort worth"]),
}
RANK_WEIGHTS = [34, 30, 24, 18, 14, 12, 10, 8]
CLEARANCE_ONLY = ["security clearance", "clearance required", "ts/sci", "top secret", "secret clearance",
                  "polygraph", "active clearance"]
LANGS = ["python", "java", "go", "golang", "rust", "c++", "c#", "typescript", "javascript", "kotlin",
         "swift", "scala", "ruby", "sql", "node"]


# ---------------------------------------------------------------- prompts

def ask(q, default=""):
    hint = f" [{default}]" if default else ""
    try:
        a = input(f"{q}{hint}: ").strip()
    except EOFError:
        sys.exit("\nsetup cancelled")
    return a or default


def ask_yes(q, default=True):
    a = ask(f"{q} ({'Y/n' if default else 'y/N'})").lower()
    return default if not a else a.startswith("y")


def ask_choice(q, options, default):
    keys = list(options)
    print(q)
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {options[k]}")
    while True:
        a = ask("choose a number", str(keys.index(default) + 1))
        if a.isdigit() and 1 <= int(a) <= len(keys):
            return keys[int(a) - 1]


def ask_multi(q, options, defaults):
    keys = list(options)
    print(q)
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {options[k]}")
    d = ",".join(str(keys.index(k) + 1) for k in defaults if k in keys)
    while True:
        a = ask("numbers, comma-separated, in order of preference", d)
        picks = [x.strip() for x in a.split(",") if x.strip()]
        if picks and all(p.isdigit() and 1 <= int(p) <= len(keys) for p in picks):
            return list(dict.fromkeys(keys[int(p) - 1] for p in picks))


def ask_month(q, default):
    while True:
        a = ask(f"{q} (YYYY-MM)", default)
        if re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", a):
            return a


def section(title):
    print(f"\n{'─' * 64}\n{title}\n{'─' * 64}")


# ---------------------------------------------------------------- API key

def load_env():
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "ANTHROPIC_API_KEY" and v.strip() and not os.environ.get(k.strip()):
                os.environ["ANTHROPIC_API_KEY"] = v.strip()


def get_api_key():
    """Return True once a working ANTHROPIC_API_KEY is in the environment, asking for it if needed.
    The key is checked with a free Models API call and saved to .env (mode 600)."""
    load_env()
    if os.environ.get("ANTHROPIC_API_KEY"):
        if not ENV.exists():
            print("  using ANTHROPIC_API_KEY from your environment (a scheduled run needs it set too,\n"
                  "  or put it in .env)")
        return True
    try:
        import anthropic
    except ImportError:
        print("  The AI features need the anthropic SDK: create the venv (see README) and run\n"
              "  .venv/bin/python setup.py")
        return False
    print("  Get a key at console.anthropic.com → API Keys (add a few dollars of credit, and a\n"
          "  monthly spend limit under Limits). Input is hidden.")
    for _ in range(3):
        key = getpass.getpass("  ANTHROPIC_API_KEY (Enter to skip): ").strip()
        if not key:
            return False
        try:
            anthropic.Anthropic(api_key=key).models.retrieve("claude-haiku-5-5")
        except anthropic.AuthenticationError:
            print("  that key was rejected; try again")
            continue
        except anthropic.APIError as e:
            print(f"  couldn't verify the key ({type(e).__name__}); saving it anyway")
        ENV.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
        ENV.chmod(0o600)
        os.environ["ANTHROPIC_API_KEY"] = key
        print(f"  ✓ key saved to {ENV.name} (gitignored, readable only by you)")
        return True
    return False


# Measured on the held-out test set (39 labeled postings, F-1 profile); see README "Evaluation".
SCREENING_CHOICE = """\
Eligibility screening reads each shortlisted JD and drops roles you can't take
(no sponsorship, citizens-only, export-controlled). Two engines:

  rules (free)      67% correct · wrongly dropped 7 of 25 eligible jobs · let 6 of 14 restricted through
  AI agents         97% correct · wrongly dropped 0 of 25               · let 1 of 14 restricted through
                    ~$0.008 per JD checked (Claude Haiku, escalating to Opus only when unsure)

Each JD is checked once and cached. At a typical ~45 new shortlisted postings a day that's about
$0.40/day (~$11/month); the first run checks your whole backlog once (roughly $0.50-1)."""


# ---------------------------------------------------------------- resume

def resume_text(path):
    """Plain text from .txt/.md/.docx, or from .pdf via pdftotext if installed. None if unreadable."""
    suf = path.suffix.lower()
    if suf in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suf == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))
    if suf == ".pdf" and shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True)
        return r.stdout or None
    return None


def parse_resume_with_claude(path):
    """Structured fields from the resume via Claude. Returns a dict, or None if unavailable."""
    try:
        from typing import Literal

        import anthropic
        from pydantic import BaseModel, ConfigDict
    except ImportError:
        print("  (anthropic / pydantic not installed: run with .venv/bin/python to enable this)")
        return None

    class Resume(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str
        degree_level: Literal["bachelor", "master", "phd"]
        degree: str
        graduation: str
        skills: list[str]
        role_families: list[Literal[tuple(FAMILIES)]]

    if path.suffix.lower() == ".pdf":
        doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                              "data": base64.standard_b64encode(path.read_bytes()).decode()}}
    else:
        text = resume_text(path)
        if not text:
            return None
        doc = {"type": "text", "text": f"<resume>\n{text}\n</resume>"}
    families = "; ".join(f"{k} = {v['label']}" for k, v in FAMILIES.items())
    prompt = ("Extract these fields from the resume above. degree: the degree currently being "
              "pursued (or most recent), e.g. 'MS in Computer Science, <University>'. graduation: its "
              "expected graduation month as YYYY-MM, empty string if not stated. skills: programming "
              "languages, frameworks, databases, and tools, as written. role_families: the 1-4 families "
              f"the experience points to, from: {families}.")
    print("  reading resume with Claude…")
    resp = anthropic.Anthropic().messages.parse(
        model="claude-opus-5-5", max_tokens=16000,
        messages=[{"role": "user", "content": [doc, {"type": "text", "text": prompt}]}],
        output_format=Resume, output_config={"effort": "low"},
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        return None
    return resp.parsed_output.model_dump()


def read_resume():
    path = ask("Path to your resume (PDF / DOCX / TXT), or Enter to skip")
    if not path:
        return {}
    path = Path(os.path.expanduser(path.strip("'\""))).resolve()
    if not path.exists():
        print(f"  not found: {path}; skipping")
        return {}
    print("  Claude can read it and pre-fill your answers (~$0.02). This sends the file to the Anthropic API.")
    if ask_yes("  Use AI parsing?", True) and get_api_key():
        try:
            info = parse_resume_with_claude(path)
        except Exception as e:  # network / API errors shouldn't end setup
            print(f"  resume parsing failed ({type(e).__name__}: {e})")
            info = None
        if info:
            print(f"  found: {info['degree']} · graduating {info['graduation'] or '?'} · "
                  f"{len(info['skills'])} skills")
            return info
    text = resume_text(path) or ""
    found = [l for l in LANGS if re.search(rf"(?<![\w+#]){re.escape(l)}(?![\w+#])", text, re.I)]
    if found:
        print(f"  found languages in the text: {', '.join(found)}")
    return {"skills": found} if found else {}


# ---------------------------------------------------------------- build

def candidate_phrase(auth, deg):
    return {
        "us_citizen": "a US citizen",
        "permanent_resident": "a US permanent resident",
        "f1": f"an F-1 {DEGREE[deg].lower()} student who would work under CPT",
        "h1b": "an H-1B visa holder who would need the employer to file an H-1B transfer",
        "other_sponsorship": "a candidate who needs employer visa sponsorship to work in the US",
    }[auth]


def build_profile(a):
    import tracker  # for the default LA city list
    p = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    needs = a["auth"] in ("f1", "h1b", "other_sponsorship")
    us_person = a["auth"] in ("us_citizen", "permanent_resident")

    p["_comment"] = f"Profile for {a['name'] or 'the user'}, written by setup.py on {date.today()}. Re-run setup.py or edit by hand."
    p["season_year"] = a["season"]
    c = p["candidate"]
    c.update({
        "name": a["name"], "work_authorization": a["auth"], "needs_sponsorship": needs,
        "degree_level": a["degree_level"], "degree": a["degree"], "graduation": a["graduation"],
        "graduation_earliest": a["grad_lo"], "graduation_latest": a["grad_hi"],
        "target_roles": a["families"], "available_terms": a["terms"], "stack": a["skills"],
    })
    metros = [m for m in a["locations"] if m != "remote"]
    if metros:
        label, cities = METROS[metros[0]]
        c["preferred_area"] = {"label": label, "cities": cities or tracker.DEFAULT_LA}
    else:
        c["preferred_area"] = {"label": "Remote", "cities": ["remote"]}

    p["screening"]["candidate"] = candidate_phrase(a["auth"], a["degree_level"])
    p["screening"]["export_licenses_unlikely"] = a["export_unlikely"]
    p["screening"]["engine"] = a["engine"]

    ex = p["hard_exclude"]
    ex["markers"] = (["🛂"] if needs else []) + ([] if a["auth"] == "us_citizen" else ["🇺🇸"]) + ["🔒"]
    ex["exclude_no_h1b_sponsor"] = needs
    ex["role_keywords"] = [] if a["auth"] == "us_citizen" else (CLEARANCE_ONLY if us_person else ex["role_keywords"])
    if not a["exclude_defense"]:
        ex["companies"] = []
    if us_person:
        p["soft_warn"]["companies"], p["soft_warn"]["keywords"] = [], []
    ex["max_age_days"] = a["max_age"]
    ex["min_term_year"] = a["season"]

    titles = list(ex["title_mismatch_regex"])
    for k in a["families"]:
        f = FAMILIES[k]
        if not f.get("scope"):
            continue
        ex["non_swe_role_keywords"] = [x for x in ex["non_swe_role_keywords"] if x not in f.get("keywords", [])]
        ex["non_swe_role_regex"] = [x for x in ex["non_swe_role_regex"] if x not in f.get("regex", [])]
        p["swe_include_keywords"] += [x for x in f.get("include", []) if x not in p["swe_include_keywords"]]
        if "title" in f:
            titles[f["title"]] = None
        if "jd" in f:
            ex["jd_mismatch"][f["jd"]] = False
    ex["title_mismatch_regex"] = [t for t in titles if t]

    sc = p["scoring"]
    stack = {}
    for k in a["families"]:
        stack.update(FAMILIES[k]["score"])
    for s in a["skills"]:
        if s.lower() in LANGS:
            stack.setdefault(s.lower(), 10)
    sc["stack_match"] = {"_comment": "title mentions one of these => fits the target roles / skills", **stack}
    locs = {}
    for rank, m in enumerate(a["locations"]):
        w = RANK_WEIGHTS[min(rank, len(RANK_WEIGHTS) - 1)]
        for city in (["remote"] if m == "remote" else (METROS[m][1] or tracker.DEFAULT_LA)):
            locs.setdefault(city, w)
    sc["locations"] = locs
    sc["_la_note"] = "Location weights follow your ranking in setup.py: earlier = higher."
    if not needs:
        sc["linkedin_unverified_penalty"] = 0
    if a["degree_level"] == "bachelor":
        sc["advanced_degree_bonus"] = sc["grad_title_bonus"] = 0
    p["cooldown"]["groups"] = []
    return p


# ---------------------------------------------------------------- main

def main():
    if not EXAMPLE.exists():
        sys.exit(f"missing {EXAMPLE}")
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else None
    oc = (old or {}).get("candidate", {})
    print("Internship Agent setup. Press Enter to accept a [default].")
    if old:
        print("(An existing profile was found; its answers are the defaults.)")

    section("1/3  About you")
    a = {}
    r = read_resume()
    a["name"] = ask("Name (stays on this machine)",
                    r.get("name") or (oc.get("name", "") if oc.get("name") != "Your Name" else ""))
    a["auth"] = ask_choice("Work authorization in the US:", AUTH, oc.get("work_authorization", "f1"))
    a["export_unlikely"] = False
    if a["auth"] not in ("us_citizen", "permanent_resident"):
        print("Roles with ITAR/EAR export-controlled access need a US export license for non-US persons,\n"
              "and licenses are rarely granted for some nationalities (e.g. China, Russia, Iran).")
        a["export_unlikely"] = ask_yes("Is that likely the case for your nationality?", True)
    a["degree_level"] = ask_choice("Degree you're pursuing:", DEGREE,
                                   r.get("degree_level") or oc.get("degree_level", "master"))
    a["degree"] = ask("Degree and school", r.get("degree") or (oc.get("degree", "") if "<" not in oc.get("degree", "<") else ""))
    a["graduation"] = ask_month("Expected graduation", r.get("graduation") or oc.get("graduation", "")[:7]
                                or f"{date.today().year + 1}-05")
    print("Job postings often state a graduation window. If you could graduate earlier or later, widen it.")
    a["grad_lo"] = ask_month("  earliest possible graduation", a["graduation"])
    a["grad_hi"] = ask_month("  latest possible graduation", a["graduation"])

    section("2/3  What you're looking for")
    fam_opts = {k: f["label"] + ("" if not f.get("scope") else "  (excluded unless picked)")
                for k, f in FAMILIES.items()}
    a["families"] = ask_multi("Which kinds of roles? (general software engineering is always included)",
                              fam_opts, r.get("role_families") or oc.get("target_roles") or DEFAULT_FAMILIES)
    skills = ask("Your skills (comma-separated)", ", ".join(r.get("skills") or oc.get("stack", [])))
    a["skills"] = [s.strip() for s in skills.split(",") if s.strip()]
    loc_opts = {"remote": "Remote", **{k: v[0] for k, v in METROS.items()}}
    a["locations"] = ask_multi("Where do you want to work? Pick in order of preference (others still appear, ranked lower).",
                               loc_opts, ["remote", "la", "sf", "nyc"])
    terms = ask_multi("Which terms can you intern?", {"Summer": "Summer", "Fall": "Fall", "Spring": "Spring",
                                                      "Winter": "Winter"}, ["Summer", "Fall", "Spring", "Winter"])
    a["terms"] = terms + ["Co-op"]
    t = date.today()
    a["season"] = int(ask("Recruiting season (internship year)", str(t.year + 1 if t.month >= 6 else t.year)))
    a["exclude_defense"] = ask_yes("Hide defense / aerospace contractors?", a["auth"] != "us_citizen")
    a["max_age"] = int(ask("Hide postings older than N days", "10"))

    a["engine"] = "regex"
    if a["auth"] == "us_citizen":
        print("\nAs a US citizen no sponsorship / citizenship / export rule applies, so eligibility\n"
              "screening is skipped (dead links and off-target titles are still checked).")
    else:
        print("\n" + SCREENING_CHOICE)
        default_ai = (old or {}).get("screening", {}).get("engine", "cascade") == "cascade"
        if ask_yes("Use AI screening?", default_ai) and get_api_key():
            a["engine"] = "cascade"

    section("3/3  Review")
    us = a["auth"] in ("us_citizen", "permanent_resident")
    print(f"  {a['name'] or '(no name)'} · {AUTH[a['auth']]} · {DEGREE[a['degree_level']]} · "
          f"graduating {a['graduation']} (window {a['grad_lo']}..{a['grad_hi']})")
    print(f"  roles: {', '.join(FAMILIES[k]['label'] for k in a['families'])}")
    print(f"  skills: {', '.join(a['skills']) or '-'}")
    print(f"  locations: {' > '.join(loc_opts[k] for k in a['locations'])}")
    print(f"  terms: {', '.join(terms)} {a['season']} · postings ≤ {a['max_age']} days")
    print("  screening: " + ("no sponsorship checks (not needed)" if us else
                             "drops roles with no sponsorship / citizens-only / export-controlled access, "
                             + ("with AI agents" if a["engine"] == "cascade" else "with rules")))
    if not ask_yes("Write this profile?", True):
        sys.exit("nothing written")

    if OUT.exists():
        shutil.copy(OUT, OUT.with_suffix(".json.bak"))
    OUT.write_text(json.dumps(build_profile(a), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ wrote {OUT.relative_to(ROOT)}")
    print("\nNext:\n  bash tracker/run_daily.sh     # fetch, filter, screen, build the report\n"
          "  bash tracker/open_panel.sh    # dashboard")


if __name__ == "__main__":
    main()
