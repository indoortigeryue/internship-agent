"""The agents and the orchestrator.

    Extractor ──▶ grounding check (code) ──▶ Verifier (LLM) ──▶ accept
        ▲                 │                        │
        └──── feedback ◀──┴────────────────────────┘   (max MAX_ROUNDS)

- Extractor reads a JD and states the sponsorship / citizenship restrictions,
  each with a verbatim quote.
- Grounding check is plain code: every quote must literally occur in the JD.
  It's free, deterministic, and catches hallucinated evidence before we pay
  for a second model to look at it.
- Verifier is a separate model call (optionally a different vendor) that
  judges whether each quote really supports the claim and hunts for
  restrictions the extractor missed.
- Disagreement goes back to the Extractor as feedback (evaluator-optimizer).
"""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

import llm

MAX_ROUNDS = 3
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"

# Who we screen for comes from the tracker's profile (written by setup.py), not from code.
# Without one (e.g. re-scoring the published eval), fall back to the example profile.
_TRACKER = Path(__file__).resolve().parent.parent / "tracker"
PROFILE = next(p for p in (_TRACKER / "profile.json", _TRACKER / "profile.example.json") if p.exists())
_profile = json.loads(PROFILE.read_text(encoding="utf-8"))
_screening = _profile["screening"]
AUTH = _profile["candidate"].get("work_authorization", "f1")
NEEDS_SPONSOR = _profile["candidate"].get("needs_sponsorship", True)
_EXPORT_RULE = """
  If the posting says THIS position requires access to export-controlled
  (ITAR/EAR) information, that is "required" even when it adds "or be
  eligible to obtain an export license": licenses for the candidate's
  nationality are rarely granted, and employers routinely decline to apply
  for them.""" if _screening["export_licenses_unlikely"] else ""


def dedupe(text):
    """Page text from the tracker's fetchers (verify.page_text) is the JSON-LD
    description followed by the whole page body, so the JD appears twice plus
    site chrome (LinkedIn sign-in, "similar jobs"). Keep the first copy: cut
    where its opening 200 chars show up again."""
    text = text.strip()
    head = text[:200]
    i = text.find(head, 1000) if len(head) == 200 else -1
    return text[:i].strip() if i > 0 else text


class Extraction(BaseModel):
    # Field order is generation order: reasoning comes first so the model reasons before it
    # commits to a label. With reasoning last, Haiku once wrote a correct argument for
    # none_stated *after* having already emitted no_sponsorship.
    model_config = ConfigDict(extra="forbid")
    reasoning: str
    sponsorship: Literal["none_stated", "no_sponsorship", "fulltime_only"]
    sponsorship_quote: str
    citizenship: Literal["none_stated", "required", "conditional_or_boilerplate"]
    citizenship_quote: str


class FieldCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["sponsorship", "citizenship"]
    correct: bool
    problem: str


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[FieldCheck]
    missed_restriction_quote: str
    approved: bool


RULES = f"""\
The candidate is {_screening["candidate"]}. CPT is already valid US work
authorization, so plain "must be authorized to work in the US" is NOT a
restriction. What matters:

sponsorship
- no_sponsorship: the employer will not sponsor / support visas or work
  authorization for THIS role, now or in the future (e.g. "we are unable to
  sponsor", "must not require sponsorship now or in the future").
- fulltime_only: the no-sponsorship statement explicitly applies only to
  full-time / post-graduation employment, not the internship itself.
  ("full-time internship" is still the internship.)
- none_stated: nothing like the above.

citizenship
- required: a hard requirement for US citizenship, permanent residency,
  "US person" status, or an active security clearance for this role.{_EXPORT_RULE}
- conditional_or_boilerplate: export-control or clearance language that is
  hedged ("may require", "some positions", "offer may be conditioned on",
  "if access ... is required") or is a generic company-wide disclaimer.
- none_stated: nothing like the above.
- A requirement that explicitly admits F-1/CPT master's students (e.g.
  "US citizen, permanent resident, or MS student on CPT") is none_stated.

Only the job description itself counts. Ignore text about OTHER jobs (e.g.
"similar jobs" lists, other postings' titles), site navigation, and footers.
"""

EXTRACTOR_SYSTEM = f"""You screen internship job descriptions for an international student.

{RULES}
For each field, quote the single most relevant sentence from the JD VERBATIM
(copy it exactly, no paraphrase, no ellipses). Use an empty string when the
value is none_stated. Start with one or two sentences of reasoning."""

VERIFIER_SYSTEM = f"""You audit another model's reading of an internship job description.

{RULES}
For each field, decide whether the extracted value is correct given the full
JD. Check especially: is a quoted restriction actually hedged or scoped to
full-time roles? Does it come from the job itself rather than another
posting? Then search the JD for any restriction the extraction MISSED and
quote it verbatim (empty string if none). Approve only if both fields are
correct and nothing was missed. In `problem`, say concretely what is wrong
(empty string if correct)."""


_F1_RULES = RULES


def _rules_for(auth):
    """RULES above is written for an F-1 student (the evaluated configuration).
    Other statuses swap the parts that depend on it."""
    r = _F1_RULES
    f1_intro = r[:r.index("What matters:")]
    f1_clause = r[r.index("- A requirement that explicitly admits"):r.index("Only the job description")]
    if auth in ("h1b", "other_sponsorship"):
        r = r.replace(f1_intro, f"The candidate is {_screening['candidate']}. They need the employer "
                      "to sponsor (or transfer) a work visa, so a statement that the employer won't "
                      "sponsor applies. Restrictions that name only OTHER visa types (e.g. \"we can't "
                      "support J-1 students\") do not apply to this candidate. "
                      "Plain \"must be authorized to work in the US\" without mention "
                      "of sponsorship is NOT a restriction. ").replace(f1_clause, "\n")
    elif auth in ("permanent_resident", "us_citizen"):
        sponsorship = r[r.index("sponsorship\n"):r.index("citizenship\n")]
        hard = r[r.index("- required:"):r.index("- conditional_or_boilerplate:")]
        r = (r.replace(f1_intro, f"The candidate is {_screening['candidate']}: no sponsorship is "
                       "needed, and they count as a \"US person\" under export-control rules. ")
             .replace(sponsorship, "sponsorship\n- always none_stated: sponsorship does not apply "
                      "to this candidate.\n\n")
             .replace(hard, "- required: a hard requirement for US citizenship specifically, or an "
                      "active security clearance. \"US person\", permanent-resident, or ITAR/EAR "
                      "requirements do NOT exclude this candidate.\n")
             .replace(f1_clause, "\n"))
    return r


RULES = _rules_for(AUTH)
EXTRACTOR_SYSTEM = EXTRACTOR_SYSTEM.replace(_F1_RULES, RULES)
VERIFIER_SYSTEM = VERIFIER_SYSTEM.replace(_F1_RULES, RULES)
if AUTH != "f1":
    EXTRACTOR_SYSTEM = EXTRACTOR_SYSTEM.replace("for an international student", "for a job seeker")


def verdict(e: Extraction) -> str:
    # Same precedence as the regex baseline (tracker/verify.py checks
    # sponsorship first) so 3-class labels are comparable.
    if e.sponsorship == "no_sponsorship" and NEEDS_SPONSOR:
        return "no_sponsorship"
    if e.citizenship == "required":
        return "citizen_only"
    return "eligible"


def _norm(s):
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9']+", " ", s).strip()


def grounding_problems(e: Extraction, text: str) -> list[str]:
    jd = _norm(text)
    out = []
    for field, quote in (("sponsorship", e.sponsorship_quote), ("citizenship", e.citizenship_quote)):
        if quote and _norm(quote) not in jd:
            out.append(f"{field}: the quote {quote!r} does not appear verbatim in the JD. "
                       "Quote the JD exactly, or set the field to none_stated if no such sentence exists.")
    return out


def _jd_block(job):
    return f"Company: {job['company']}\nTitle: {job['title'] or job['role']}\n\n<jd>\n{job['text']}\n</jd>"


def extract(job, model, feedback=None, prev=None):
    user = _jd_block(job)
    if feedback:
        user += ("\n\nYour previous extraction:\n" + prev.model_dump_json(indent=1) +
                 "\n\nA reviewer found these problems:\n- " + "\n- ".join(feedback) +
                 "\n\nRe-read the JD and produce a corrected extraction.")
    return llm.call(model, EXTRACTOR_SYSTEM, user, Extraction)


def verify(job, ext, model):
    user = _jd_block(job) + "\n\nExtraction to audit:\n" + ext.model_dump_json(indent=1)
    return llm.call(model, VERIFIER_SYSTEM, user, Review)


def screen(job, extractor_model, verifier_model=None, grounding=True):
    """Orchestrator. Returns the final result plus a step-by-step trace."""
    steps = []
    ext, tr = extract(job, extractor_model)
    steps.append({"agent": "extractor", "round": 1, **tr})
    rounds = 1
    while True:
        problems = grounding_problems(ext, job["text"]) if grounding else []
        if problems:
            steps.append({"agent": "grounding", "round": rounds, "problems": problems})
        elif verifier_model:
            rev, tr = verify(job, ext, verifier_model)
            steps.append({"agent": "verifier", "round": rounds, "approved": rev.approved, **tr})
            if not rev.approved:
                problems = [f"{c.field}: {c.problem}" for c in rev.checks if not c.correct and c.problem]
                if rev.missed_restriction_quote:
                    problems.append(f"missed restriction: {rev.missed_restriction_quote!r}")
                problems = problems or ["the reviewer did not approve; re-check both fields"]
        if not problems or rounds >= MAX_ROUNDS:
            break
        rounds += 1
        ext, tr = extract(job, extractor_model, feedback=problems, prev=ext)
        steps.append({"agent": "extractor", "round": rounds, **tr})

    return {"id": job["id"], "verdict": verdict(ext), "extraction": ext.model_dump(),
            "rounds": rounds, "unresolved": bool(problems),
            "cost": round(sum(s.get("cost", 0) for s in steps), 6),
            "latency_s": round(sum(s.get("latency_s", 0) for s in steps), 2),
            "steps": steps}


# ── Cascade: cheap triage model, expensive specialist only when needed ──────
#
#   Haiku triage ──▶ router (code) ──clean──▶ accept Haiku's answer
#                        │
#                        └──uncertain──▶ Opus specialist (fresh read) ──▶ accept
#
# The specialist does not see the triage answer, so it can't anchor on it.

# Phrases that signal a possible restriction. Only used as a safety net for
# when triage says "nothing here" — "citizen" alone is left out because every
# EEO statement lists "citizenship" as a protected class.
RISK = re.compile(
    r"sponsor|\bvisa\b|export[- ]control|\bITAR\b|\bEAR\b|U\.?S\.? persons?\b|"
    r"security clearance|permanent resident|green card|\bCPT\b|\bOPT\b|"
    r"must be (?:a )?U\.?S\.? citizen|citizenship (?:is )?required", re.I)


def route(e: Extraction, text: str) -> str | None:
    """Why the triage answer can't be trusted on its own (None = trust it)."""
    if grounding_problems(e, text):
        return "triage quote not found in JD"
    if e.sponsorship == "fulltime_only" or e.citizenship == "conditional_or_boilerplate":
        return "nuanced call (scoped or hedged restriction)"
    if e.citizenship == "required":
        return "hard citizenship / export-control claim"
    if e.sponsorship == "none_stated" and e.citizenship == "none_stated" and RISK.search(text):
        return "triage found nothing but JD has restriction language"
    return None  # clean: grounded no_sponsorship, or eligible with no risk phrases


def cascade(job, triage_model, specialist_model):
    ext, tr = extract(job, triage_model)
    steps = [{"agent": "triage", **tr}]
    why = route(ext, job["text"])
    steps.append({"agent": "router", "escalate": why})
    if why:
        ext, tr = extract(job, specialist_model)
        steps.append({"agent": "specialist", **tr})
    return {"id": job["id"], "verdict": verdict(ext), "extraction": ext.model_dump(),
            "rounds": 1, "escalated": bool(why), "unresolved": False,
            "cost": round(sum(s.get("cost", 0) for s in steps), 6),
            "latency_s": round(sum(s.get("latency_s", 0) for s in steps), 2),
            "steps": steps}
