#!/usr/bin/env python3
"""Sync applied records from the application-log spreadsheet into seen.json.

Key check: the same company + title can span two recruiting seasons (last year's
application was for Summer 2026, the current posting is Summer 2027 -- different jobs,
so they can't both be marked applied). So a job is marked only when **the application
date >= the job's posting date**, meaning that exact posting was applied to.

Usage:
    python3 import_applied.py            # dry run, print only
    python3 import_applied.py --write    # actually write seen.json
"""

import argparse
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
# Application log: an .xlsx with columns Company Name / Title / Application Date.
XLSX = ROOT / "applications.xlsx"
SHEET = "Applied"
SEEN = ROOT / "data" / "seen.json"
JOBS = ROOT / "data" / "jobs.json"

# An xlsx is just XML inside a zip, so the standard library is enough.
# Deliberately avoids openpyxl -- this runs daily from a scheduled task and can't depend on
# third-party packages installed in a temp directory.
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_RNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def read_sheet(path, sheet_name):
    """Read all rows of the given sheet; each row is a list of strings."""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target")
                for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        target = None
        for sh in wb.find(f"{NS}sheets"):
            if sh.get("name") == sheet_name:
                target = rels[sh.get(f"{RNS}id")]
                break
        if target is None:
            raise KeyError(f"Sheet not found: {sheet_name}")
        target = "xl/" + target.lstrip("/").removeprefix("xl/")

        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))

        rows = []
        for row in ET.fromstring(z.read(target)).iter(f"{NS}row"):
            cells = {}
            for c in row.iter(f"{NS}c"):
                col = re.match(r"([A-Z]+)", c.get("r") or "A").group(1)
                idx = 0
                for ch in col:
                    idx = idx * 26 + ord(ch) - 64
                v, t = c.find(f"{NS}v"), c.get("t")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    is_ = c.find(f"{NS}is")
                    val = "".join(x.text or "" for x in is_.iter(f"{NS}t")) if is_ is not None else ""
                else:
                    val = v.text if v is not None else ""
                cells[idx - 1] = (val or "").strip()
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows


def excel_serial_to_date(n):
    """Excel dates are serial numbers: days since 1899-12-30."""
    try:
        return date(1899, 12, 30) + timedelta(days=int(float(n)))
    except (TypeError, ValueError):
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


STOP = {"intern", "internship", "summer", "winter", "spring", "fall", "opportunities",
        "for", "university", "students", "the", "and", "of", "a", "co", "op", "ms",
        "bs", "phd", "2026", "2027", "2028", "new", "grad"}


def tok(s):
    return [w for w in norm(s).split() if w not in STOP]


def similar(a, b):
    """Title similarity. 'Software Engineering Intern, MS, Summer 2027' and
    'Software Engineer Intern' are the same job, so drop season/degree noise words
    and let same-prefix words like engineer / engineering match."""
    ta, tb = tok(a), tok(b)
    if not ta or not tb:
        return 0.0
    def eq(x, y):
        return x == y or (len(x) >= 5 and len(y) >= 5 and (x.startswith(y[:5]) or y.startswith(x[:5])))
    hits = sum(1 for x in ta if any(eq(x, y) for y in tb))
    overlap = hits / min(len(ta), len(tb))
    return max(overlap, SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio())


TERM_RE = re.compile(r"\b(summer|winter|spring|fall|autumn)\b[^a-z0-9]{0,6}(20\d\d)?", re.I)


def terms(s):
    """Extract the term(s) from a title, e.g. 'Summer 2027' / 'Winter/Spring 2027'.

    E.g. an application to a Tesla Winter/Spring 2027 role must not mark Tesla's
    Summer 2027 posting -- different jobs; if the terms don't match, it isn't applied.
    """
    out = set()
    for m in TERM_RE.finditer(s or ""):
        out.add(m.group(1).lower())
    return out


def parse_app_date(raw, today):
    """Dates in the spreadsheet are month.day like '11.13' / '9.4' with no year --
    a month later than the current one is taken as last year.

    Also accepts Excel serial numbers and '2/13/2025'-style dates.
    """
    raw = str(raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", raw):          # Excel date serial number
        return excel_serial_to_date(raw)
    m3 = re.match(r"(\d{1,2})/(\d{1,2})/(20\d\d)$", raw)
    if m3:
        try:
            return date(int(m3.group(3)), int(m3.group(1)), int(m3.group(2)))
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})[./](\d{1,2})$", raw)
    if not m:
        return None
    mo, dy = int(m.group(1)), int(m.group(2))
    year = today.year if mo <= today.month else today.year - 1
    try:
        return date(year, mo, dy)
    except ValueError:
        return None


def read_applications(today):
    if not XLSX.exists():
        print(f"no application log at {XLSX}, skipping")
        return []
    rows = read_sheet(XLSX, SHEET)
    if not rows:
        return []
    hdr = rows[0]
    ci = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        def cell(name):
            i = ci.get(name)
            return r[i] if i is not None and i < len(r) else ""
        comp, title = cell("Company Name"), cell("Title")
        if not comp or not title:
            continue
        d = parse_app_date(cell("Application Date"), today)
        if d:
            out.append({"company": comp.strip(), "role": title.strip(), "date": d})
    return out


def main(write):
    today = date.today()
    apps = read_applications(today)
    jobs = json.loads(JOBS.read_text(encoding="utf-8"))
    seen = json.loads(SEEN.read_text(encoding="utf-8"))

    by_company = {}
    for a in apps:
        by_company.setdefault(norm(a["company"]), []).append(a)

    marked, skipped_newer = [], []
    for j in jobs:
        if seen.get(j["key"], {}).get("status") == "applied":
            continue
        cj = norm(j["company"])
        cands = [a for ck, lst in by_company.items() if ck and (ck in cj or cj in ck) for a in lst]
        if not cands:
            continue
        # One company can have several records (applied last year and again this year).
        # Whether *this* posting was applied to depends on **whether any similar-enough
        # application happened after it was posted**, so scan candidates newest-first
        # rather than only the closest title match.
        tj = terms(j["role"])
        hits = []
        for a in cands:
            ta = terms(a["role"])
            if tj and ta and not (tj & ta):
                continue          # terms explicit and different -> not the same job
            r = similar(j["role"], a["role"])
            if r >= 0.8:   # at 0.79 false matches start (Factory Software vs Vehicle QA)
                hits.append((a, r))
        if not hits:
            continue
        hits.sort(key=lambda x: (-x[0]["date"].toordinal(), -x[1]))
        posted = today - timedelta(days=j["age_days"]) if j.get("age_days") is not None else None
        newest, r_new = hits[0]
        if posted and newest["date"] < posted:
            skipped_newer.append((j, newest, posted))
            continue
        marked.append((j, newest, r_new))

    print(f"The sheet has {len(apps)} application records; the tracker has {len(jobs)} jobs\n")
    print(f"=== Marked as applied ({len(marked)}) ===")
    for j, a, r in sorted(marked, key=lambda x: -x[0]["score"]):
        print(f"  ✓ {j['company']} — {j['role']}")
        print(f"      ← matches what you applied to on {a['date']}: {a['role']}  (similarity {r:.2f})")
    if skipped_newer:
        print(f"\n=== Same company and title, but this posting is newer than your application; kept as pending ({len(skipped_newer)}) ===")
        for j, a, posted in skipped_newer[:20]:
            print(f"  ○ {j['company']:20} | {j['role'][:46]:46} | you applied on {a['date']}, but this was only posted {posted} (newer)")

    if write:
        for j, a, _ in marked:
            if j["key"] in seen:
                seen[j["key"]]["status"] = "applied"
                seen[j["key"]]["applied_date"] = a["date"].isoformat()
        SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nWrote seen.json; {len(marked)} jobs marked as applied")
    else:
        print("\n(dry run, nothing written. Add --write to apply)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
