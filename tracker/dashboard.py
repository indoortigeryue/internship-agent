#!/usr/bin/env python3
"""Render data/jobs.json into a clickable HTML panel.

Usage: python3 dashboard.py   ->  dashboard.html
"""

import html
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tracker as _t  # noqa: E402  profile loading, preferred area, scoring helpers
JOBS = json.loads((ROOT / "data" / "jobs.json").read_text(encoding="utf-8"))
PROFILE = _t.load_profile()
MUST = PROFILE["buckets"]["must_apply"]
WORTH = PROFILE["buckets"]["worth_applying"]

# seen.json is the source of truth for status, not jobs.json --
# jobs.json is written by tracker.py, but import_applied.py / verify.py run after it
# and only update seen.json. Reading jobs.json alone would miss jobs removed by verification.
SEEN = json.loads((ROOT / "data" / "seen.json").read_text(encoding="utf-8"))
VERIFIED = {}
vf = ROOT / "data" / "verified.json"
if vf.exists():
    VERIFIED = json.loads(vf.read_text(encoding="utf-8"))

for j in JOBS:
    j["status"] = SEEN.get(j["key"], {}).get("status", j["status"])
    v = VERIFIED.get(j["url"], {})
    j["verdict"] = v.get("verdict", "")
    j["flag"] = v.get("flag", "").replace("soft_", "")
    j["flag_note"] = v.get("flag_note", "")
    # simplify normalizes role names and wipes out key details
    # (Tesla's "Update Systems Validation" became "Software Engineer Intern")
    rt = v.get("real_title", "")
    j["real_title"] = rt if rt and rt.lower() not in j["role"].lower() else ""

# Manually added jobs render straight from manual_jobs.json without waiting for a tracker rerun --
# otherwise clicking "add" on the panel means waiting ~40s for a full fetch before the card shows.
MANUAL_FILE = ROOT / "manual_jobs.json"
manual_extra = []
if MANUAL_FILE.exists():
    known_urls = {j["url"] for j in JOBS if j.get("url")}
    known_ats = {_t.ats_id(j.get("url")) for j in JOBS if _t.ats_id(j.get("url"))}
    for m in json.loads(MANUAL_FILE.read_text(encoding="utf-8")):
        if m.get("_comment") or m.get("url") in known_urls:
            continue
        if _t.ats_id(m.get("url")) and _t.ats_id(m["url"]) in known_ats:
            continue
        j = {
            "key": _t.norm_key(m["company"], m["role"]),
            "company": m["company"], "role": m["role"],
            "location": m.get("location", ""), "url": m.get("url", ""),
            "markers": [], "sources": ["manual"], "section": "Manually added",
            "age_days": 0, "salary": m.get("salary", ""),
            "requirements": m.get("requirements", []),
            "note": m.get("note", ""), "force": True,
            "first_seen": m.get("added", ""), "status": "shown",
            "itar_risk": _t.itar_risk({"company": m["company"], "role": m["role"]}, PROFILE),
        }
        j["score"], j["why"] = _t.score(j, PROFILE, {})
        j["grad"], j["grad_note"] = _t.grad_fit(j["requirements"], PROFILE)
        j["is_la"] = any(c in j["location"].lower() for c in _t.PREF_CITIES)
        j["verdict"] = j["flag"] = j["flag_note"] = j["real_title"] = ""
        manual_extra.append(j)

pending = [j for j in JOBS if j["status"] == "shown"] + manual_extra
pending.sort(key=lambda j: (not j["is_la"], -j["score"]))


def esc(s):
    return html.escape(str(s or ""))


def clean_note(s):
    """Last-line cleanup before rendering. ATSes like Workday put **double-escaped** HTML in JSON
    (`&lt;h2&gt;Job Title:&lt;/h2&gt;`), which flows into grad_note / flag_note
    and shows up on the page as literal &lt;p&gt;. Fixed at the source, but guarded here too --
    stale data may remain in jobs.json, or appear from other sources later."""
    s = str(s or "")
    for _ in range(2):
        if "&lt;" in s or "&#" in s or "&amp;" in s:
            s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def trim(s, n):
    """Truncate on a word boundary so we never cut mid-word."""
    s = clean_note(s)
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut) + "…"


def tags(j):
    out = []
    if j.get("force"):
        out.append('<span class="tag manual">📌 Manually added</span>')
    if "linkedin" in j.get("sources", []):
        out.append('<span class="tag li">🔗 LinkedIn 24h</span>')
    if j.get("pay_range"):
        out.append(f'<span class="tag pay">💰 {esc(j["pay_range"])}</span>')
    for qf in j.get("quality_flags") or []:
        out.append(f'<span class="tag warn">⚑ {esc(qf)}</span>')
    if j.get("repost_suspect"):
        n = j.get("applicants")
        out.append(f'<span class="tag warn">🔁 Likely repost'
                   + (f' ({n} applicants)' if n else '') + '</span>')
    if j["is_la"]:
        out.append(f'<span class="tag la">📍 {esc(_t.PREF_LABEL)}</span>')
    if j["score"] >= MUST:
        out.append('<span class="tag must">🔥 Must apply</span>')
    elif j["score"] >= WORTH:
        out.append('<span class="tag worth">✅ Worth applying</span>')
    if "🔥" in j["markers"]:
        out.append('<span class="tag faang">FAANG+</span>')
    if j.get("grad") == "fit":
        out.append('<span class="tag grad">🎓 Grad date fits</span>')
    if j.get("salary"):
        out.append(f'<span class="tag pay">💰 {esc(j["salary"])}</span>')
    if j.get("age_days") is not None and j["age_days"] <= 3:
        out.append(f'<span class="tag fresh">Posted within {j["age_days"]}d</span>')
    if j.get("itar_risk"):
        out.append('<span class="tag warn">⚠️ Aerospace/defense: confirm US-person requirement</span>')
    if j.get("flag"):
        out.append(f'<span class="tag warn">⚑ JD mentions {esc(j["flag"])}</span>')
    if j.get("verdict") in ("unknown", "blocked"):
        why = "page needs JS rendering" if j["verdict"] == "unknown" else "site has anti-bot protection"
        out.append(f'<span class="tag warn">🔍 JD not auto-verified ({why}); check sponsorship terms yourself</span>')
    elif j.get("verdict") == "ok":
        out.append('<span class="tag worth">✓ JD verified</span>')
    return "".join(out)


def card(j):
    grad = (f'<div class="grad-note">🎓 {esc(trim(j["grad_note"], 200))}</div>'
            if j.get("grad_note") else "")
    apply_btn = (f'<a class="apply" href="{esc(j["url"])}" target="_blank" rel="noopener">Apply →</a>'
                 if j["url"] else '<span class="apply off">No direct link</span>')
    return f"""
<div class="card" data-key="{esc(j['key'])}" data-la="{int(j['is_la'])}"
     data-score="{j['score']}" data-grad="{1 if j.get('grad') == 'fit' else 0}"
     data-manual="{1 if j.get('force') else 0}"
     data-li="{1 if 'linkedin' in j.get('sources', []) else 0}"
     data-text="{esc((j['company'] + ' ' + j['role'] + ' ' + j['location']).lower())}">
  <div class="row1">
    <label class="done"><input type="checkbox" class="chk"><span>Applied</span></label>
    <div class="titles">
      <div class="company">{esc(j['company'])}</div>
      <div class="role">{esc(j['role'])}</div>
      {f'<div class="realtitle">Actual title: {esc(j["real_title"])}</div>' if j.get('real_title') else ''}
    </div>
    <div class="score">{j['score']}</div>
  </div>
  <div class="tags">{tags(j)}</div>
  <div class="loc">📍 {esc(j['location'][:220])}</div>
  <div class="why">{esc(' · '.join(j['why'][:4]))}</div>
  {grad}
  {f'<div class="flag-note">⚑ {esc(trim(j["flag_note"].lstrip(), 180))}</div>' if j.get('flag_note') else ''}
  {f'<div class="my-note">📌 {esc(clean_note(j["note"]))}</div>' if j.get('note') else ''}
  {f'<div class="flag-note">🔁 {esc(j["repost_note"])} (LinkedIn guest API does not expose the Reposted label; inferred from applicant count)</div>' if j.get('repost_note') else ''}
  <div class="foot">{apply_btn}
    <button class="del" data-company="{esc(j['company'])}" data-role="{esc(j['role'])}"
            data-url="{esc(j['url'])}" data-jkey="{esc(j['key'])}"
            data-manual="{1 if j.get('force') and 'manual' in j.get('sources', []) else 0}"
            title="Not interested / added by mistake">✕ Delete</button>
    <span class="src">{esc('/'.join(j['sources']))} · first seen {esc(j['first_seen'])}</span></div>
</div>"""


la_n = sum(1 for j in pending if j["is_la"])
must_n = sum(1 for j in pending if j["score"] >= MUST)
grad_n = sum(1 for j in pending if j.get("grad") == "fit")

HTML = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Internship dashboard · {date.today()}</title>
<style>
 :root {{ --bg:#f7f8fa; --card:#ffffff; --line:#e2e5ea; --fg:#1c2024; --dim:#5f6773;
          --accent:#1668d6; --la:#c2410c; --must:#c62828; --ok:#0f7a52; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
        font:14px/1.5 -apple-system,"SF Pro Text","PingFang SC",sans-serif; }}
 h1 {{ font-size:20px; margin:0 0 4px; }}
 .sub {{ color:var(--dim); margin-bottom:18px; }}
 .bar {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center;
         position:sticky; top:0; background:var(--bg); padding:10px 0 14px; z-index:5;
         border-bottom:1px solid var(--line); margin-bottom:16px; }}
 .filter {{ padding:6px 13px; border:1px solid var(--line); border-radius:999px;
            background:var(--card); color:var(--fg); cursor:pointer; font-size:13px; }}
 .filter.on {{ border-color:var(--accent); color:var(--accent); background:#eaf2fd; font-weight:600; }}
 #q {{ flex:1; min-width:180px; padding:7px 12px; border-radius:8px;
       border:1px solid var(--line); background:var(--card); color:var(--fg); }}
 #count {{ color:var(--dim); font-size:13px; }}
 .grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(430px,1fr)); }}
 .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px;
           box-shadow:0 1px 2px rgba(16,24,40,.05); }}
 .card.hidden {{ display:none; }}
 .card.applied {{ opacity:.45; background:#f1f3f6; }}
 .row1 {{ display:flex; gap:10px; align-items:flex-start; }}
 .titles {{ flex:1; min-width:0; }}
 .company {{ font-weight:600; font-size:15px; }}
 .role {{ color:var(--dim); }}
 .score {{ font-variant-numeric:tabular-nums; color:var(--accent); font-weight:600; }}
 .done {{ display:flex; align-items:center; gap:4px; font-size:11px; color:var(--dim);
          cursor:pointer; user-select:none; flex-direction:column; }}
 .tags {{ display:flex; gap:5px; flex-wrap:wrap; margin:9px 0; }}
 .tag {{ font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line);
         color:var(--dim); background:#f4f6f8; white-space:nowrap; }}
 .tag.la {{ color:var(--la); border-color:#f0c3a6; background:#fff3ec; font-weight:600; }}
 .tag.must {{ color:var(--must); border-color:#f3bfbf; background:#fdf0f0; }}
 .tag.worth {{ color:var(--ok); border-color:#a9d9c5; background:#eef8f3; }}
 .tag.grad, .tag.pay, .tag.fresh {{ color:var(--fg); }}
 .tag.li {{ color:#0a66c2; border-color:#a8cdf0; background:#eef5fc; font-weight:600; }}
 .tag.manual {{ color:#5b21b6; border-color:#c4b5fd; background:#f5f3ff; font-weight:600; }}
 .tag.warn {{ color:#8a5a00; border-color:#e8cf9a; background:#fdf6e6; }}
 .loc, .why {{ color:var(--dim); font-size:12.5px; margin-bottom:5px; }}
 .realtitle {{ font-size:12px; color:#8a5a00; background:#fdf6e6; border-radius:4px;
                padding:2px 7px; margin-top:4px; display:inline-block; }}
 .my-note {{ font-size:12px; color:#4c1d95; background:#f5f3ff; border-left:3px solid #a78bfa;
              padding:5px 8px; border-radius:5px; margin:6px 0; }}
 .flag-note {{ font-size:11.5px; color:#6b6357; background:#faf8f3; border-left:3px solid #e8cf9a;
               padding:5px 8px; border-radius:5px; margin:6px 0; }}
 .grad-note {{ font-size:12px; color:#33413a; background:#eef8f3; border-left:3px solid var(--ok);
               padding:6px 9px; border-radius:5px; margin:7px 0; }}
 .foot {{ display:flex; align-items:center; gap:10px; margin-top:10px; }}
 .apply {{ background:var(--accent); color:#fff; padding:7px 16px; border-radius:8px;
           text-decoration:none; font-weight:600; }}
 .apply.off {{ background:#eceef1; color:var(--dim); }}
 .src {{ color:#8a919c; font-size:11px; }}
 .del {{ background:transparent; border:1px solid var(--line); color:var(--dim);
         border-radius:8px; padding:6px 11px; cursor:pointer; font-size:12px; }}
 .del:hover {{ border-color:var(--must); color:var(--must); }}
 #addbox {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; width:100%;
            padding:10px; background:#fff; border:1px solid var(--line); border-radius:10px;
            margin-bottom:14px; }}
 #addurl {{ flex:1; min-width:260px; padding:8px 12px; border-radius:8px;
            border:1px solid var(--line); font-size:13px; }}
 #addnote {{ width:170px; padding:8px 12px; border-radius:8px; border:1px solid var(--line);
             font-size:13px; }}
 #addbtn {{ background:var(--ok); color:#fff; border:none; border-radius:8px;
            padding:9px 18px; cursor:pointer; font-weight:600; }}
 #addbtn:disabled {{ opacity:.5; cursor:default; }}
 #addmsg {{ font-size:12.5px; color:var(--dim); flex-basis:100%; }}
 #banner {{ display:none; padding:10px 14px; border-radius:10px; margin-bottom:12px;
             background:#fff8e6; border:1px solid #e8cf9a; color:#7a5800; font-size:13px; }}
 .offline #banner {{ display:block; }}
 #banner code {{ background:#fff; padding:2px 6px; border-radius:4px; border:1px solid #e8cf9a; }}
 #synccmd {{ margin-left:8px; padding:4px 10px; border-radius:6px; cursor:pointer;
             border:1px solid #c99; background:#fff; font-size:12px; }}
 .card.pending-del {{ opacity:.35; }}
 #copy {{ padding:6px 13px; border-radius:8px; border:1px solid var(--ok);
          background:transparent; color:var(--ok); cursor:pointer; font-size:13px; }}
</style></head><body>

<h1>Internship dashboard</h1>
<div class="sub">{date.today()} · To apply: {len(pending)} · {esc(_t.PREF_LABEL)}: {la_n} · Must apply: {must_n} · Grad date fits: {grad_n}</div>

<div id="banner">
  This is an <b>offline preview</b> (the html file opened directly), so adds and deletes are only queued in your browser.
  To make changes take effect, run <code>python3 panel.py</code> in a terminal, then open this dashboard at
  <code>http://localhost:8765</code>.
  <button id="synccmd">📋 Copy pending sync commands</button>
</div>

<div id="addbox">
  <input id="addurl" placeholder="Paste a job application link, then press Enter or click Add…">
  <input id="addnote" placeholder="Note (optional)">
  <button id="addbtn">+ Add job</button>
  <div id="addmsg"></div>
</div>

<div class="bar">
  <button class="filter on" data-f="all">All</button>
  <button class="filter" data-f="la">📍 {esc(_t.PREF_LABEL)}</button>
  <button class="filter" data-f="must">🔥 Must apply</button>
  <button class="filter" data-f="grad">🎓 Grad date fits</button>
  <button class="filter" data-f="linkedin">🔗 LinkedIn</button>
  <button class="filter" data-f="manual">📌 Manually added</button>
  <button class="filter" data-f="todo">Not applied</button>
  <input id="q" placeholder="Search company / role / city…">
  <button id="copy">Copy mark-applied commands</button>
  <span id="count"></span>
</div>

<div class="grid">{''.join(card(j) for j in pending)}</div>

<script>
const KEY = 'applied-jobs';
const applied = new Set(JSON.parse(localStorage.getItem(KEY) || '[]'));
const cards = [...document.querySelectorAll('.card')];

cards.forEach(c => {{
  const k = c.dataset.key, box = c.querySelector('.chk');
  if (applied.has(k)) {{ box.checked = true; c.classList.add('applied'); }}
  box.addEventListener('change', () => {{
    box.checked ? applied.add(k) : applied.delete(k);
    c.classList.toggle('applied', box.checked);
    localStorage.setItem(KEY, JSON.stringify([...applied]));
    render();
  }});
}});

let mode = 'all';
const q = document.getElementById('q');

function render() {{
  let shown = 0;
  cards.forEach(c => {{
    const t = q.value.trim().toLowerCase();
    let ok = !t || c.dataset.text.includes(t);
    if (ok && mode === 'la')   ok = c.dataset.la === '1';
    if (ok && mode === 'must') ok = +c.dataset.score >= {MUST};
    if (ok && mode === 'grad') ok = c.dataset.grad === '1';
    if (ok && mode === 'manual') ok = c.dataset.manual === '1';
    if (ok && mode === 'linkedin') ok = c.dataset.li === '1';
    if (ok && mode === 'todo') ok = !applied.has(c.dataset.key);
    c.classList.toggle('hidden', !ok);
    if (ok) shown++;
  }});
  document.getElementById('count').textContent = `Showing ${{shown}} · Applied ${{applied.size}}`;
}}

document.querySelectorAll('.filter').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('.filter').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); mode = b.dataset.f; render();
}}));
q.addEventListener('input', render);

// The dashboard state lives in localStorage and the tracker's in seen.json; these commands sync the two
document.getElementById('copy').addEventListener('click', () => {{
  const names = cards.filter(c => applied.has(c.dataset.key))
                     .map(c => c.querySelector('.company').textContent.trim());
  if (!names.length) return alert("You haven't checked any jobs as applied yet");
  const cmd = [...new Set(names)]
    .map(n => `python3 tracker.py --applied "${{n}}"`).join('\\n');
  navigator.clipboard.writeText(cmd)
    .then(() => alert('Copied ' + [...new Set(names)].length + ' command(s). Paste them into a terminal and run to sync.'))
    .catch(() => prompt('Copy manually:', cmd));
}});

// Add / delete need to write files, which the browser can't do, so they go through the local panel.py.
// When dashboard.html is opened directly there is no backend, and these controls are hidden automatically (body.offline).
async function api(path, body) {{
  const r = await fetch(path, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}}

fetch('/ping').then(r => {{ if (!r.ok) throw 0; }})
              .catch(() => document.body.classList.add('offline'));

const addUrl = document.getElementById('addurl');
const addNote = document.getElementById('addnote');
const addBtn = document.getElementById('addbtn');
const addMsg = document.getElementById('addmsg');

// Offline: queue operations and offer a copyable command.
// An earlier version hid the buttons when there was no backend, so the feature
// didn't exist in the one place the user actually looks (the opened html file).
const QKEY = 'pending-ops';
const queue = JSON.parse(localStorage.getItem(QKEY) || '[]');
const saveQueue = () => localStorage.setItem(QKEY, JSON.stringify(queue));

function shq(x) {{ return "'" + String(x).replace(/'/g, "'\\\\''") + "'"; }}

function refreshQueueHint() {{
  const b = document.getElementById('synccmd');
  b.textContent = queue.length ? `📋 Copy pending sync commands (${{queue.length}})`
                               : '📋 Nothing to sync';
}}

document.getElementById('synccmd').addEventListener('click', () => {{
  if (!queue.length) return;
  const lines = queue.map(op => op.type === 'add'
    ? `python3 add_job.py ${{shq(op.url)}}` + (op.note ? ` --note ${{shq(op.note)}}` : '')
    : `python3 remove_job.py ${{shq(op.url || op.company)}} --yes`);
  const cmd = 'cd {ROOT}\\n' + lines.join('\\n')
            + '\\npython3 dashboard.py';
  navigator.clipboard.writeText(cmd)
    .then(() => {{
      if (confirm(`Copied ${{queue.length}} command(s). After running them in a terminal, click "OK" to clear the queue.`)) {{
        queue.length = 0; saveQueue(); refreshQueueHint();
      }}
    }})
    .catch(() => prompt('Copy manually:', cmd));
}});

async function doAdd() {{
  const url = addUrl.value.trim();
  if (!url) return;
  if (!url.startsWith('http')) {{ addMsg.textContent = 'Link must start with http'; return; }}
  addBtn.disabled = true;
  addMsg.textContent = 'Fetching job info…';
  try {{
    const res = await api('/add', {{url, note: addNote.value.trim()}});
    addMsg.textContent = `Added: ${{res.company}} — ${{res.role}}`
      + (res.warn ? ` ⚠️ ${{res.warn}}` : '') + '. Reloading…';
    setTimeout(() => location.reload(), 1200);
  }} catch (e) {{
    queue.push({{type: 'add', url, note: addNote.value.trim()}});
    saveQueue(); refreshQueueHint();
    addMsg.textContent = 'Queued (this is an offline preview). Click "Copy pending sync commands" above '
      + 'to get the commands, or run python3 panel.py and open the dashboard at localhost:8765 to apply directly.';
    addUrl.value = ''; addNote.value = '';
    addBtn.disabled = false;
  }}
}}
addBtn.addEventListener('click', doAdd);
addUrl.addEventListener('keydown', e => {{ if (e.key === 'Enter') doAdd(); }});

document.querySelectorAll('.del').forEach(b => b.addEventListener('click', async () => {{
  const d = b.dataset;
  if (!confirm(`Delete "${{d.company}} — ${{d.role}}"?\nIt won't show up in the list again.`)) return;
  b.disabled = true; b.textContent = 'Deleting…';
  try {{
    await api('/remove', {{company: d.company, role: d.role, url: d.url,
                          key: d.jkey, manual: d.manual === '1'}});
    b.closest('.card').remove();
    render();
  }} catch (e) {{
    // No backend: gray out the card and queue the op; "Copy pending sync commands" applies it later
    queue.push({{type: 'remove', company: d.company, role: d.role, url: d.url}});
    saveQueue(); refreshQueueHint();
    b.closest('.card').classList.add('pending-del');
    b.textContent = 'Delete pending sync';
  }}
}}));

refreshQueueHint();
render();
</script></body></html>"""

out = ROOT / "dashboard.html"
out.write_text(HTML, encoding="utf-8")
print(f"To apply: {len(pending)} ({_t.PREF_LABEL} {la_n} / Must apply {must_n} / Grad date fits {grad_n})")
print(f"Dashboard: {out}")
