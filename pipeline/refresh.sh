#!/usr/bin/env bash
# Daily Health OS refresh (run by Heath's 08:00 "Morning Brief" cron job; its stdout is injected into the brief).
# Glooko export -> WHOOP history sync -> DEXA sync -> generate_dashboard.py -> Ladder log archive -> portal publish
# -> commit the legacy dashboard data if it changed.
set -euo pipefail

PIPE=/root/health-dashboard/pipeline
REPO=/root/health-dashboard
LOG=/tmp/health-dashboard-refresh.log
# The pipeline has its own HOME holding the GitHub CLI login (and nothing else), so it does not depend on Scout's profile.
export HOME=/root/health-pipeline-home
export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd "$REPO"

"$PIPE/glooko_export_refresh.py" > "$LOG" 2>&1
# Quiet unless something changes or fails; their output reaches the Morning Brief.
"$PIPE/whoop_history_sync.py" || true
"$PIPE/dexa_sync.py" || true
./generate_dashboard.py >> "$LOG" 2>&1
# Sweep Ladder Workout Log entries older than 30 days into the monthly archive.
"$PIPE/ladder_log_archive.py" >> "$LOG" 2>&1
# Build and publish the Health Portal (docs/index.html + docs/portal_data.json). Quiet unless it fails.
"$PIPE/publish_portal.sh" || true

# Commit the legacy dashboard data only if it changed.
if git diff --quiet -- data/dashboard_data.json docs/data.json docs/index.html; then
  exit 0
fi
git add data/dashboard_data.json docs/data.json docs/index.html
git commit -m "Refresh health dashboard data" >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1

python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path('/root/health-dashboard/data/dashboard_data.json').read_text())
s = data.get('summary30', {})
daily = sorted(data.get('daily90', []), key=lambda r: r.get('local_date', ''))
def avg_tir(days):
    vals = [r.get('tir_70_180') for r in daily[-days:] if r.get('tir_70_180') is not None]
    return round(sum(vals) / len(vals), 1) if vals else None
def fmt(v, suffix=''):
    return '—' if v is None else f'{v}{suffix}'
print('Health Dashboard refreshed and published.')
print(f"Data range: {data.get('date_range',{}).get('start')} → {data.get('date_range',{}).get('end')}")
print(f"TIR: 3d {fmt(avg_tir(3), '%')} | 7d {fmt(avg_tir(7), '%')} | 30d {fmt(s.get('tir_70_180'), '%')}")
print(f"Avg glucose: {fmt(s.get('avg_glucose'), ' mg/dL')} | WHOOP recovery: {fmt(s.get('avg_recovery'))}")
PY
