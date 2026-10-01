#!/usr/bin/env python3
"""Save Heath's short morning note for the Health Portal.

Usage: write_heath_note.py "sentence one" "sentence two" ...  (1–5 plain sentences)
"""
import json, re, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

lines = [re.sub(r"[*_`#>]+", "", a).strip() for a in sys.argv[1:] if a.strip()]
if not 1 <= len(lines) <= 5:
    sys.exit("Give 1 to 5 sentences as separate arguments.")
now = datetime.now(ZoneInfo("America/Los_Angeles"))
note = {"date": now.date().isoformat(), "when": now.strftime("%-I:%M %p"), "lines": [l[:400] for l in lines]}
Path("/root/health-portal/heath_note.json").write_text(json.dumps(note, indent=2))
print("Saved portal note for", note["date"])
