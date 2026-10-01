#!/usr/bin/env bash
# Build the Health Portal data + page and publish to GitHub Pages (docs/). Silent on success;
# prints one line on failure so the no-agent cron job alerts David.
set -euo pipefail
LOG=/tmp/health-portal-publish.log
trap 'echo "Health Portal publish failed at $(date +%H:%M). See $LOG on the server."' ERR
PY=/usr/local/lib/hermes-agent/venv/bin/python
REPO=/root/health-dashboard
export HOME=/root/health-pipeline-home
export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
$PY "$REPO/pipeline/build_portal_data.py" "$REPO/docs/portal_data.json" > "$LOG" 2>&1
$PY "$REPO/pipeline/portal/build_index.py" "$REPO/docs/index.html" >> "$LOG" 2>&1
cd "$REPO"
git add docs/index.html docs/portal_data.json
if git diff --cached --quiet; then exit 0; fi
git commit -q -m "Update health portal" >> "$LOG" 2>&1
git push -q origin main >> "$LOG" 2>&1
