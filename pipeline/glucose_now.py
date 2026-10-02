#!/usr/bin/env python3
"""Heath's on-demand live glucose check: fetches the latest Dexcom Share readings and prints the context
block (current value, trend, 20-min projection, last 3 hours, today so far, last boluses, workouts).

Usage: python3 /root/health-dashboard/pipeline/glucose_now.py
Runs with Hermes' Python; pydexcom lives in /root/health-pipeline-home/pylib.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_glucose as L

try:
    L.fetch(minutes=180)
    note = ""
except L.NoLogin as e:
    note = f"Live feed not set up: {e}. Showing stored readings only."
except Exception as e:
    note = f"Dexcom Share unreachable right now ({type(e).__name__}). Showing stored readings only."
rows = L.recent(180)
s = L.stats(rows)
if note:
    print(note)
print(L.context_block(rows, s) if s else "No live readings in the last 3 hours.")
