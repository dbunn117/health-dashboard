#!/usr/bin/env python3
"""Keep a permanent local copy of David's WHOOP recovery, sleep and workout records.

The WHOOP API is paged; generate_dashboard.py used to pull only the latest 25 records and
rebuild health.sqlite each run, so history was lost. This script upserts raw records into
/root/health-data/whoop/whoop_history.sqlite, which generate_dashboard.py now reads.

Usage: whoop_history_sync.py            # refresh the last 21 days
       whoop_history_sync.py --backfill # fetch everything since 2015
"""
import json, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/root/health-data/whoop/whoop_history.sqlite")
HOME = "/root/.hermes/profiles/heath/home"
CLI = f"{HOME}/.local/bin/whoop-pp-cli"
KINDS = {
    "recovery": (["recovery"], "cycle_id", "created_at"),
    "sleep": (["activity", "get-sleep-collection"], "id", "end"),
    "workout": (["activity", "get-workout-collection"], "id", "start"),
}


def fetch(cmd, start, end):
    out, token = [], None
    for _ in range(500):
        args = [CLI, *cmd, "--json", "--no-input", "--yes", "--data-source", "live", "--no-cache",
                "--limit", "25", "--start", start, "--end", end, "--timeout", "120s"]
        if token:
            args += ["--next-token", token]
        cp = subprocess.run(args, text=True, capture_output=True, timeout=180, env={"HOME": HOME, "PATH": "/usr/local/bin:/usr/bin:/bin"})
        if cp.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {(cp.stderr or cp.stdout).strip()[:300]}")
        res = json.loads(cp.stdout).get("results") or {}
        recs = res.get("records") or []
        out += recs
        token = res.get("next_token")
        if not token or not recs:
            break
    return out


def main():
    backfill = "--backfill" in sys.argv
    now = datetime.now(timezone.utc)
    start = "2015-01-01T00:00:00Z" if backfill else (now - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    summary = []
    for kind, (cmd, key, tcol) in KINDS.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {kind}(id TEXT PRIMARY KEY, t TEXT, json TEXT)")
        recs = fetch(cmd, start, end)
        con.executemany(f"INSERT OR REPLACE INTO {kind} VALUES (?,?,?)",
                        [(str(r.get(key)), r.get(tcol), json.dumps(r, separators=(",", ":"))) for r in recs if r.get(key) is not None])
        total, first = con.execute(f"SELECT count(*), min(t) FROM {kind}").fetchone()
        summary.append(f"{kind}: fetched {len(recs)}, stored {total} since {str(first)[:10]}")
    con.commit()
    if backfill or "--verbose" in sys.argv:
        print("; ".join(summary))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # stay quiet for cron unless something breaks
        print(f"WHOOP history sync failed: {e}")
        sys.exit(1)
