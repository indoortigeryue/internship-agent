#!/usr/bin/env python3
"""Watch the companies in watchlist.json that the 4 sources don't cover, by hitting their own ATS directly.

Why a separate layer: tracker.py's 4 sources have systematic gaps -- Bloomberg / Jane Street /
Stripe have 0 entries in the SimplifyJobs and vanshb03 READMEs (checked 2026-09-16),
so the daily report would give no signal on the day they open roles.

Core rule: when the number of want_re matches goes **from 0 to non-zero**, the role is open --
that day's alert goes at the top of the daily report. New jobs are recorded in
data/watch_state.json and never alerted twice.

Probe self-check: each target has a control query (one that always has results). An empty
control means the probe itself is broken (stale selector / blocked / domain changed), not
"not open yet". Without this, a silently broken probe looks exactly like no openings,
and you could wait a whole season for nothing.

Usage:
    python3 watch.py            # probe, alert, write state
    python3 watch.py --dry      # probe and print only; no state write, report untouched
"""

import argparse
import html
import json
import re
import sys
import urllib.parse
from datetime import date
from pathlib import Path

import fetchlib

ROOT = Path(__file__).resolve().parent
CONF = ROOT / "watchlist.json"
STATE = ROOT / "data" / "watch_state.json"
REPORTS = ROOT / "reports"

JOBDETAIL_RE = re.compile(r"/careers/JobDetail/([^/\"']+)/(\d+)")


# ------------------------------------------------------------------ probes

def probe_greenhouse(t):
    """Return (jobs, ok). jobs is [{id,title,location,url}]; ok is the probe self-check result."""
    st, raw = fetchlib.http_get(
        f"https://boards-api.greenhouse.io/v1/boards/{t['token']}/jobs", timeout=40)
    try:
        all_jobs = json.loads(raw).get("jobs", [])
    except (ValueError, AttributeError):
        return [], False
    jobs = [{"id": str(j.get("id", "")),
             "title": (j.get("title") or "").strip(),
             "location": ((j.get("location") or {}).get("name") or "").strip(),
             "url": j.get("absolute_url", "")}
            for j in all_jobs]
    # A board with zero jobs can't be real => wrong token or the API changed
    return jobs, bool(all_jobs)


AVATURE_PAGE = 12          # Avature result pages are fixed at 12
AVATURE_MAX = 120          # paging cap, so a broken selector can't page forever


def probe_avature(t):
    """Avature search. Tested: a GET with the search param filters correctly (a gibberish query
    returns 0, results match the form POST) and supports jobOffset paging, so we use GET.

    **Paging is required**: only 12 per page, and on opening day there's likely more than one
    match; taking only the first page would miss some.

    Titles are recovered from the URL slug (the listing page's title nodes are unstable);
    slugs look like 2026-Software-Engineer-Internship-New-York, good enough for want_re.
    """
    base = t["url"].rstrip("/") + "/"

    def search(q):
        out, offset = {}, 0
        while offset < AVATURE_MAX:
            st, raw = fetchlib.http_get(base + "?" + urllib.parse.urlencode(
                {"search": q, "listFilterMode": 1, "jobOffset": offset}), timeout=40)
            if st != 200 or not raw:
                return out if offset else None      # only a first-page failure counts as probe failure
            fresh = False
            for slug, jid in JOBDETAIL_RE.findall(raw):
                if jid not in out:
                    out[jid] = {"id": jid,
                                "title": html.unescape(slug.replace("-", " ")).strip(),
                                "location": "", "url": t["job_url"].format(id=jid)}
                    fresh = True
            if not fresh:                            # nothing new on this page, we're at the end
                break
            offset += AVATURE_PAGE
        return out

    merged, any_ok = {}, False
    for q in t["queries"]:
        got = search(q)
        if got is None:
            continue
        any_ok = True
        merged.update(got)

    ctrl = search(t["control"]) if t.get("control") else None
    ok = bool(any_ok and (ctrl is None or ctrl))
    return list(merged.values()), ok


WORKDAY_LIMIT = 20        # Workday's hard limit is 20; anything larger returns empty


def probe_workday_cxs(t):
    """Workday job search API. Used by Snap (wd1.myworkdaysite.com, tenant snapchat).

    Two gotchas:
      - **limit max is 20**; passing 100 doesn't error, it returns an empty list with no total,
        which looks like "this company has zero jobs".
      - Search is fuzzy (searchText="intern" also pulls in "Internal Communications"),
        so we **fetch everything and filter locally with want_re** instead of relying on its search.
    """
    def page(offset):
        st, raw = fetchlib.http_post_json(
            t["url"], {"appliedFacets": {}, "limit": WORKDAY_LIMIT,
                       "offset": offset, "searchText": ""}, timeout=40)
        if st != 200 or not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    first = page(0)
    if not first:
        return [], False
    total = first.get("total") or 0
    posts = list(first.get("jobPostings") or [])
    offset = len(posts)
    while offset < total and offset < 1000:          # cap to keep paging from running away
        d = page(offset)
        got = (d or {}).get("jobPostings") or []
        if not got:
            break
        posts += got
        offset += len(got)

    base = t["url"].rsplit("/jobs", 1)[0]            # .../wday/cxs/<tenant>/<site>
    pub = t.get("job_url_base", base)
    jobs = []
    for p in posts:
        ids = p.get("bulletFields") or []
        jobs.append({"id": str(ids[0]) if ids else (p.get("externalPath") or "")[-24:],
                     "title": (p.get("title") or "").strip(),
                     "location": (p.get("locationsText") or "").strip(),
                     "url": pub + (p.get("externalPath") or "")})
    # total of 0 can't be real -- the API changed or we're blocked
    return jobs, bool(total and posts)


PROBES = {"greenhouse": probe_greenhouse, "avature": probe_avature,
          "workday_cxs": probe_workday_cxs}


# ------------------------------------------------------------------ main flow

def run(dry):
    conf = json.loads(CONF.read_text(encoding="utf-8"))
    want = re.compile(conf["want_re"])
    foreign = re.compile(conf["foreign_location_re"]) if conf.get("foreign_location_re") else None
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    today = date.today().isoformat()

    found, broken = [], []
    for t in conf["targets"]:
        name = t["company"]
        probe = PROBES.get(t["ats"])
        if not probe:
            print(f"  !! {name}: unknown ats type {t['ats']!r}", file=sys.stderr)
            continue

        try:
            jobs, ok = probe(t)
        except Exception as e:                      # one failing probe doesn't affect the others
            jobs, ok = [], False
            print(f"  !! {name}: probe error {type(e).__name__}: {e}", file=sys.stderr)

        hits = [j for j in jobs if want.search(j["title"])]
        prev = state.get(name, {})
        known = set(prev.get("ids", {}))
        new = [j for j in hits if j["id"] not in known]

        if not ok:
            broken.append(name)
            status = "⚠️ probe broken (not \"no openings\" - it can't be queried anymore, needs checking)"
        elif hits:
            status = f"🔥 {len(hits)} open" + (f", {len(new)} of them new" if new else " (all seen before)")
        else:
            status = f"— not open yet (scanned {len(jobs)} postings, none matched)"
        new_ids = {j["id"] for j in new}
        print(f"  {name:14} {status}")
        for j in hits:
            print(f"       {'NEW ' if j['id'] in new_ids else '    '}{j['title'][:62]}"
                  + (f"  [{j['location'][:34]}]" if j["location"] else ""))

        if hits and ok:
            found.append((t, hits, new_ids))

        if not dry:
            ids = dict(prev.get("ids", {}))
            for j in hits:
                ids.setdefault(j["id"], {"title": j["title"], "location": j["location"],
                                         "url": j["url"], "first_seen": today})
            state[name] = {"ids": ids, "last_run": today,
                           "last_ok": today if ok else prev.get("last_ok", ""),
                           "scanned": len(jobs), "hits": len(hits)}

    if not dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    if broken:
        print(f"\n  ⚠️ probe broken: {', '.join(broken)} - these can't be queried right now, "
              f"don't read this as \"not open yet\"")

    if found or broken:
        block = render(found, broken, foreign)
        print("\n" + "=" * 62)
        print(block)
        if not dry:
            append_to_report(block)
    else:
        print("\n  None of the watchlist companies have openings yet.")
    return 0


MARK_START = "<!-- watchlist:start -->"
MARK_END = "<!-- watchlist:end -->"


def render(found, broken, foreign):
    """Write **all currently open** matching jobs into the daily report, marking new ones with 🆕.

    Why not "new only": tracker.py rewrites the day's report daily, while watch.py would only
    insert content when it finds new jobs -- so a job would appear only on its first day and
    silently vanish from the report the next. Hence the full open list is output every day.

    Overseas jobs are collapsed, not dropped: the target is US roles with sponsorship, so overseas
    jobs are mostly noise, but the location field can be empty or oddly formatted, and a hard
    filter would wrongly drop some.
    """
    out = [MARK_START, "## 🔥 watchlist (companies the 4 sources don't cover, queried directly via ATS)", ""]
    for t, hits, new_ids in found:
        n = f" · 🆕 {len(new_ids)} new" if new_ids else ""
        out.append(f"### {t['company']}{n}")
        near = [j for j in hits if not (foreign and j["location"]
                                        and foreign.search(j["location"]))]
        far = [j for j in hits if j not in near]
        for j in near:
            loc = f" · {j['location']}" if j["location"] else ""
            tag = "🆕 " if j["id"] in new_ids else ""
            out.append(f"- {tag}**{j['title']}**{loc} — [Apply]({j['url']})")
        if far:
            out.append(f"- <sub>{len(far)} more overseas postings ("
                       + ", ".join(j["location"][:22] for j in far) + "), collapsed</sub>")
        if t.get("action"):
            out.append(f"- ⚡ **What to do**: {t['action']}")
        out.append("")
    if broken:
        out += [f"> ⚠️ **probe broken**: {', '.join(broken)}. "
                f"These can't be queried anymore, which **does not mean they have no openings** - check whether "
                f"the token / url in watchlist.json has expired.", ""]
    out.append(MARK_END)
    return "\n".join(out)


def append_to_report(block):
    """Put the watchlist block at the top of today's report. Comment markers delimit it, so reruns
    replace the block instead of stacking. If the report doesn't exist yet, write a separate file
    rather than silently dropping it."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    p = REPORTS / f"{date.today().isoformat()}.md"
    if not p.exists():
        p2 = REPORTS / f"{date.today().isoformat()}_watchlist.md"
        p2.write_text(block + "\n", encoding="utf-8")
        print(f"  Today's report doesn't exist yet, wrote the watchlist separately to: {p2.name}")
        return
    old = p.read_text(encoding="utf-8")
    i, k = old.find(MARK_START), old.find(MARK_END)
    if i != -1 and k != -1:
        new = old[:i] + block + old[k + len(MARK_END):]
        action = "Updated the watchlist block in the report"
    else:
        new = block + "\n\n---\n\n" + old
        action = "Inserted the watchlist at the top of the report"
    p.write_text(new, encoding="utf-8")
    print(f"  {action}: {p.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print only; don't write state or touch the report")
    a = ap.parse_args()
    sys.exit(run(a.dry))
