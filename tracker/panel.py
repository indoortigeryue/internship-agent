#!/usr/bin/env python3
"""Job panel with a backend. Start it, open http://localhost:8765, and add/remove jobs right on the page.

Why this exists: `dashboard.html` is a static file and the browser can't write to disk.
When opened directly, the page falls back to offline mode (the frontend adds body.offline if /ping fails).
Run this script to make edits on the page take effect.

Standard library only, no Flask or similar -- this runs daily and shouldn't depend on packages in a temp env.

Usage:
    python3 panel.py            # start the server and open the browser
    python3 panel.py --port 9000
    python3 panel.py --no-open
"""

import argparse
import json
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DASH = ROOT / "dashboard.html"
MANUAL = ROOT / "manual_jobs.json"
EXCLUDED = ROOT / "manual_excluded.json"
SEEN = ROOT / "data" / "seen.json"

LOCK = threading.Lock()          # several handler threads modify the same JSON files


def regenerate():
    """Regenerate dashboard.html. Reads local JSON only, no network, fast."""
    subprocess.run([sys.executable, "dashboard.py"], cwd=ROOT,
                   capture_output=True, timeout=120)


def load_manual():
    if not MANUAL.exists():
        return []
    return [j for j in json.loads(MANUAL.read_text(encoding="utf-8"))
            if not j.get("_comment")]


def save_manual(items):
    header = {"_comment": "Manually added jobs. tracker.py loads this as a 5th source on every rerun; "
                          "the force flag skips all hard filters. Maintained by add_job.py / panel.py."}
    MANUAL.write_text(json.dumps([header] + items, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")


def do_add(url, note):
    import add_job
    import verify
    items = load_manual()
    if any(j["url"] == url for j in items):
        return {"ok": False, "error": "This URL has already been added"}

    status, raw, api_title, meta = verify.fetch_jd(url)
    text = verify.page_text(raw) if raw and not raw.startswith("__ERROR__") else ""
    title = api_title or (verify.real_title(raw) if text else "")

    warn = ""
    if len(text) > 400:
        for label, pats in (("The JD seems to say sponsorship is not offered", verify.NO_SPONSOR),
                            ("The JD seems to require citizenship / security clearance", verify.CITIZEN_ONLY)):
            if verify.find_first(text, pats):
                warn = label
                break

    entry = {
        "company": add_job.guess_company(url, raw or "", text),
        "role": title or "(role title unknown, set it with add_job.py --role)",
        "location": meta.get("location") or add_job.guess_location(raw or "", text),
        "url": url, "salary": add_job.guess_salary(text), "note": note,
        "added": date.today().isoformat(),
        "requirements": [text[:4000]] if text else [],
    }
    items.append(entry)
    save_manual(items)
    return {"ok": True, "company": entry["company"], "role": entry["role"], "warn": warn}


def do_remove(company, role, url, key, is_manual):
    import remove_job
    from tracker import role_base

    if is_manual:
        items = load_manual()
        save_manual([j for j in items if j.get("url") != url])

    # Jobs from sources must go on the blocklist -- otherwise the next tracker run fetches them again.
    # Block by "company + role name without term", not by key: the key changes if upstream edits the title.
    if not is_manual:
        data = remove_job.load_excluded()
        data["excluded"].append({
            "company": company, "role": role,
            "base": f"{remove_job.norm_company(company)}::{role_base(role)}",
            "url": url, "reason": "removed from the dashboard",
            "removed": date.today().isoformat(),
        })
        remove_job.save_excluded(data)

    if SEEN.exists() and key:
        seen = json.loads(SEEN.read_text(encoding="utf-8"))
        if key in seen:
            seen[key]["status"] = "skipped"
            seen[key]["skip_reason"] = "removed from the dashboard"
            SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):          # default access log is too noisy
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            return self._send(200, '{"ok":true}')
        if self.path in ("/", "/index.html", "/dashboard.html"):
            if not DASH.exists():
                regenerate()
            return self._send(200, DASH.read_bytes(), "text/html")
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, '{"error":"Malformed request"}')

        try:
            with LOCK:
                if self.path == "/add":
                    url = (payload.get("url") or "").strip()
                    if not url.startswith("http"):
                        return self._send(400, json.dumps(
                            {"error": "URL must start with http"}, ensure_ascii=False))
                    res = do_add(url, (payload.get("note") or "").strip())
                    if not res.get("ok"):
                        return self._send(400, json.dumps(res, ensure_ascii=False))
                elif self.path == "/remove":
                    res = do_remove(payload.get("company", ""), payload.get("role", ""),
                                    payload.get("url", ""), payload.get("key", ""),
                                    bool(payload.get("manual")))
                else:
                    return self._send(404, '{"error":"not found"}')
                regenerate()
            self._send(200, json.dumps(res, ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                       ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    regenerate()

    # **Must listen on both IPv4 and IPv6 loopback.** On macOS, Chrome resolves localhost to
    # ::1, so binding only 127.0.0.1 gives ERR_EMPTY_RESPONSE in the browser --
    # while curl 127.0.0.1 works, which makes it easy to misread as the server being down.
    # Use two loopback listeners instead of binding "::", which would expose the panel to the LAN.
    servers = []
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            cls = type("Srv", (ThreadingHTTPServer,),
                       {"address_family": family, "daemon_threads": True})
            servers.append(cls((host, a.port), Handler))
        except OSError as e:
            print(f"  (could not listen on {host}: {e})")
    if not servers:
        print(f"Port {a.port} could not be bound - another dashboard may already be running: pkill -f panel.py")
        return

    url = f"http://127.0.0.1:{a.port}/"
    print(f"Job dashboard: {url}")
    print(f"        {f'http://localhost:{a.port}/'}  (either address works)")
    print("Paste a URL on the page to add a job, click ✕ to remove one. Ctrl-C to quit.")
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
