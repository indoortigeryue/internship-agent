#!/bin/bash
# Daily pipeline. Scheduled runs call only this script, never the individual
# commands: tool permissions are stored per command string, so editing the
# commands in a scheduled prompt silently invalidates approvals and the
# unattended run stalls on a permission prompt. (Two runs were lost this way.)
# With the pipeline in one script, its contents can change freely.
#
#   bash run_daily.sh                  # screening engine from profile.json (setup.py)
#   ENGINE=regex bash run_daily.sh     # override for one run

set -u
cd "$(dirname "$0")" || exit 1
[ -f ../.env ] && { set -a; . ../.env; set +a; }   # ANTHROPIC_API_KEY, written by setup.py
# engine: $ENGINE overrides; otherwise whatever setup.py chose (profile.screening.engine)
ENGINE="${ENGINE:-$(python3 -c 'import json;print(json.load(open("profile.json"))["screening"].get("engine","regex"))' 2>/dev/null || echo regex)}"
VPY=python3   # the cascade engine needs the anthropic SDK from the repo's venv

# A full run can take longer than an agent's 10-minute tool limit, so a scheduled agent
# starts this script in the background and then calls the *same* command again in the
# foreground (one allowlisted command string, no new permission prompts):
#   - a run is in progress      -> wait up to ~9 min, print "DONE: ..." or "STILL RUNNING"
#   - a run finished < 30 min ago -> print "DONE: ..." without starting another one
#   - otherwise                 -> do a full run.  FORCE=1 skips both checks.
mkdir -p data
if [ -z "${FORCE:-}" ]; then
    if [ -f data/.run_pid ] && [ ! -f data/.run_done ] && kill -0 "$(cat data/.run_pid)" 2>/dev/null; then
        for _ in $(seq 1 54); do
            [ -f data/.run_done ] && { echo "DONE: $(cat data/.run_done)"; exit 0; }
            sleep 10
        done
        echo "STILL RUNNING (started $(( ($(date +%s) - $(cat data/.run_started)) / 60 )) min ago); run this command again to keep waiting"
        exit 0
    fi
    if [ -f data/.run_done ] && [ $(( $(date +%s) - $(stat -f %m data/.run_done 2>/dev/null || stat -c %Y data/.run_done) )) -lt 1800 ]; then
        echo "DONE: $(cat data/.run_done) (already finished; FORCE=1 to run again)"
        exit 0
    fi
fi
rm -f data/.run_done
echo $$ > data/.run_pid
date '+%s' > data/.run_started
trap 'echo "exit $? at $(date "+%H:%M:%S")" > data/.run_done' EXIT
[ "$ENGINE" = cascade ] && [ -x ../.venv/bin/python ] && VPY=../.venv/bin/python

echo "===== internship tracker daily pipeline $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

step() {
    echo
    echo "--- $1"
    shift
    if ! "$@"; then
        echo "!! step failed (continuing with the rest)"
        return 1
    fi
}

# LinkedIn postings from the last 24h. Must run before tracker.py: the
# data/linkedin_jobs.json it writes is tracker's 6th source. A failure here
# doesn't affect the other sources, so no exit.
step "1/7 LinkedIn last 24h" python3 linkedin.py

# Fetch + filter + score + write the daily report
step "2/7 fetch & filter" python3 tracker.py || exit 1   # nothing after this makes sense without it

# Companies the 4 sources never list (Bloomberg etc.), queried on their own ATS.
# Must run after tracker.py: alerts are inserted at the top of today's report.
step "3/7 watchlist probes" python3 watch.py

# Sync the application log (applied jobs) into seen.json
step "4/7 sync applications" python3 import_applied.py --write

# Fetch real JDs and check: dead link / no sponsorship / citizens only / role mismatch
step "5/7 JD verification" "$VPY" verify.py --write --engine "$ENGINE"

# Manually verified verdicts override the cache (must run after verify)
step "6/7 manual overrides" python3 apply_manual.py

# Build the clickable dashboard
step "7/7 dashboard" python3 dashboard.py

echo
echo "===== done $(date '+%H:%M:%S') ====="
