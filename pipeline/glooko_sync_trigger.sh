#!/usr/bin/env bash
# Log in to Glooko ~45 min before the 8:00 Morning Brief. Glooko only pulls Omnipod 5 data ~30 min after a
# login (otherwise once nightly), so this makes the 8:00 export fresh. Silent on success; prints only on failure.
LOG=/tmp/glooko-sync-trigger.log
if ! /root/health-dashboard/pipeline/glooko_export_refresh.py > "$LOG" 2>&1; then
  echo "Glooko pre-sync login failed at $(date +%H:%M) — the 8:00 Morning Brief may show stale glucose data."
  grep -v -i -E "password|token|cookie|secret" "$LOG" | tail -3
fi
