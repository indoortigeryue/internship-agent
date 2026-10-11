#!/usr/bin/env python3
"""Shared HTTP fetching layer, used by both tracker.py and verify.py.

**Why urllib alone isn't enough**: this machine's python3 is linked against LibreSSL
2.8.3, which lacks TLS 1.3. simplify.jobs sits behind Cloudflare and requires TLS 1.3,
so the handshake is rejected outright:

    [SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version

curl uses the system's newer TLS stack and fetches the same URL fine (3.7 MB).
The old SSL stack also occasionally truncates responses (on 2026-09-15 the FAANG source
was cut off mid-download, the JSON was incomplete and parsed to 0 rows, yet the log
still printed ✓).

So: try urllib first (fast, no subprocess), and fall back to curl on failure or
suspicious content.
"""

import http.client
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CURL = shutil.which("curl")


SEP = "\n__HTTP_STATUS__:"


def _run_curl(args, timeout):
    """Run curl and return (status, text). The status code is appended via -w and split off."""
    if not CURL:
        return None, ""
    try:
        p = subprocess.run([CURL, "-sSL", "--compressed", "--max-time", str(timeout),
                            "-A", UA, "-w", SEP + "%{http_code}"] + args,
                           capture_output=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return None, ""
    out = p.stdout.decode("utf-8", errors="replace")
    i = out.rfind(SEP)
    if i == -1:
        return (200 if out else None), out
    try:
        status = int(out[i + len(SEP):].strip())
    except ValueError:
        status = None
    return status, out[:i]


def _via_curl(url, timeout):
    return _run_curl([url], timeout)


def http_post_json(url, payload, timeout=45):
    """POST a JSON payload and return (status, text). curl only.

    Workday's job search endpoint (/wday/cxs/<tenant>/<site>/jobs) only accepts JSON POST;
    companies on myworkdaysite.com, such as Snap, need it to get the full job list.
    """
    return _run_curl(["-X", "POST",
                      "-H", "Content-Type: application/json",
                      "-H", "Accept: application/json",
                      "--data", json.dumps(payload), url], timeout)


def final_url(url, timeout=25):
    """Final URL after following redirects. Wrapper links like simplify.jobs/jobs/click/<uuid>
    must be resolved to learn which ATS is behind them and pick the right API.
    Returns the input unchanged on failure."""
    if not CURL:
        return url
    try:
        p = subprocess.run(
            [CURL, "-sSL", "--compressed", "--max-time", str(timeout), "-A", UA,
             "-o", "/dev/null", "-w", "%{url_effective}", url],
            capture_output=True, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return url
    out = p.stdout.decode("utf-8", errors="replace").strip()
    return out if out.startswith("http") else url


def http_get(url, timeout=45, min_bytes=0):
    """Fetch a URL and return (status, text).

    If min_bytes > 0 and urllib returns less than that, treat it as a failure and
    refetch with curl -- guards against a half-fetched response looking successful.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            text = body.decode("utf-8", errors="replace")
            if len(body) >= min_bytes:
                return r.status, text
    except urllib.error.HTTPError as e:
        # 4xx/5xx is the site's real answer; no need to retry with another transport
        return e.code, ""
    except (http.client.IncompleteRead, urllib.error.URLError, OSError):
        pass
    except Exception:
        pass
    return _via_curl(url, timeout)


def looks_truncated(text, needle_open, needle_close):
    """Rough truncation check: the opening marker is present but the closing one isn't."""
    return bool(text) and needle_open in text and needle_close not in text
