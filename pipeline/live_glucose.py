#!/usr/bin/env python3
"""Near-real-time glucose from Dexcom Share (shared by glucose_watch.py and glucose_now.py).

The Dexcom G7 phone app uploads each reading to Dexcom Share about every 5 minutes; we read it
with pydexcom (unofficial Share API, installed in /root/health-pipeline-home/pylib) and keep it in
/root/health-data/dexcom/live.sqlite. Glooko stays the source for insulin and long-term history.
Login: DEXCOM_USERNAME / DEXCOM_PASSWORD in Heath's .env (set with set_dexcom_login.sh).
"""
import json, sqlite3, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/root/health-pipeline-home/pylib")
TZ = ZoneInfo("America/Los_Angeles")
ENV = Path("/root/.hermes/profiles/heath/.env")
DIR = Path("/root/health-data/dexcom")
DB = DIR / "live.sqlite"
HEALTH_DB = Path("/root/health-dashboard/data/health.sqlite")
WHOOP_DB = Path("/root/health-data/whoop/whoop_history.sqlite")


class NoLogin(Exception):
    pass


def env(key):
    if not ENV.exists():
        return None
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def connect():
    DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS readings(epoch INTEGER PRIMARY KEY, ts TEXT, mgdl INTEGER, trend TEXT, arrow TEXT)")
    return c


def fetch(minutes=60):
    """Pull the last `minutes` of readings from Dexcom Share and upsert them. Returns the count."""
    user, pw = env("DEXCOM_USERNAME"), env("DEXCOM_PASSWORD")
    if not user or not pw:
        raise NoLogin("Dexcom login not set (run pipeline/set_dexcom_login.sh)")
    count = min(288, minutes // 5 + 2)
    dx = _client(user, pw)
    try:
        rs = dx.get_glucose_readings(minutes=minutes, max_count=count)
    except Exception:
        if not dx._cached:
            raise
        dx = _client(user, pw, fresh=True)  # cached session rejected: log in again once
        rs = dx.get_glucose_readings(minutes=minutes, max_count=count)
    _save_session(dx)
    c = connect()
    c.executemany("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?)", [
        (int(r.datetime.timestamp()), r.datetime.astimezone(TZ).isoformat(timespec="minutes"), int(r.value), r.trend_direction, r.trend_arrow)
        for r in rs])
    c.commit()
    return len(rs)


SESSION = DIR / "session.json"


def _client(user, pw, fresh=False):
    """A pydexcom client that reuses the last Share session instead of logging in every 5 minutes.
    pydexcom renews an expired session by itself; any other failure falls back to a fresh login."""
    import requests
    from pydexcom import Dexcom
    from pydexcom.const import DEXCOM_APPLICATION_IDS, DEXCOM_BASE_URLS, Region
    cached = {}
    if not fresh:
        try:
            cached = json.loads(SESSION.read_text())
        except (OSError, ValueError):
            cached = {}
    if not cached.get("session_id") or cached.get("username") != user:
        dx = Dexcom(username=user, password=pw)
        dx._cached = False
        return dx
    dx = Dexcom.__new__(Dexcom)
    dx._base_url, dx._application_id = DEXCOM_BASE_URLS[Region.US], DEXCOM_APPLICATION_IDS[Region.US]
    dx._username, dx._password, dx._account_id = user, pw, cached.get("account_id")
    dx._session_id, dx._session, dx._cached = cached["session_id"], requests.Session(), True
    return dx


def _save_session(dx):
    DIR.mkdir(parents=True, exist_ok=True)
    SESSION.write_text(json.dumps({"username": dx._username, "account_id": dx._account_id, "session_id": dx._session_id}))
    SESSION.chmod(0o600)


def recent(minutes=180):
    since = int(time.time()) - minutes * 60
    return [dict(epoch=e, ts=t, v=v, trend=tr, arrow=a)
            for e, t, v, tr, a in connect().execute("SELECT * FROM readings WHERE epoch >= ? ORDER BY epoch", (since,))]


def stats(rows):
    """Latest reading, its age, slope over the last ~15 min (mg/dL per min) and a 20-minute linear projection."""
    if not rows:
        return None
    last = rows[-1]
    win = [r for r in rows if r["epoch"] >= last["epoch"] - 16 * 60]
    slope = None
    if len(win) >= 3:
        xs = [(r["epoch"] - last["epoch"]) / 60 for r in win]
        ys = [r["v"] for r in win]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return dict(v=last["v"], arrow=last["arrow"], trend=last["trend"], epoch=last["epoch"],
                at=clock(last["epoch"]), age_min=round((time.time() - last["epoch"]) / 60),
                slope=slope, proj20=None if slope is None else round(last["v"] + slope * 20))


def clock(epoch):
    return datetime.fromtimestamp(epoch, TZ).strftime("%-I:%M %p")


def series(rows, every=15):
    """Readings thinned to one per `every` minutes, newest last: '1:45 PM 142'."""
    out, nxt = [], None
    for r in reversed(rows):
        if nxt is None or r["epoch"] <= nxt:
            out.append(f"{clock(r['epoch'])} {r['v']}")
            nxt = r["epoch"] - every * 60 + 60
    return ", ".join(reversed(out))


def today_summary():
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    vals = [v for (v,) in connect().execute("SELECT mgdl FROM readings WHERE epoch >= ?", (int(start.timestamp()),))]
    if not vals:
        return "no live readings yet today"
    tir = 100 * sum(70 <= v <= 180 for v in vals) / len(vals)
    low = 100 * sum(v < 70 for v in vals) / len(vals)
    return (f"since 12:00 AM PT: {len(vals)} readings, {tir:.0f}% in range, {low:.1f}% below 70, "
            f"average {sum(vals) / len(vals):.0f}, lowest {min(vals)}, highest {max(vals)}")


def last_boluses(n=3):
    """Most recent pump boluses Heath knows of. Comes from Glooko, so it can be hours old."""
    try:
        c = sqlite3.connect(HEALTH_DB)
        rows = c.execute("SELECT ts, insulin_delivered, carbs FROM boluses ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    except sqlite3.Error:
        return "unavailable"
    if not rows:
        return "none on record"
    return "; ".join(f"{datetime.fromisoformat(t).strftime('%a %-I:%M %p')} {u:g} U ({c:g} g carbs)" for t, u, c in rows)


def workouts_today():
    try:
        c = sqlite3.connect(WHOOP_DB)
        start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=6)
        out = []
        for t, js in c.execute("SELECT t, json FROM workout WHERE t >= ? ORDER BY t", (start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M"),)):
            w = json.loads(js)
            s = datetime.fromisoformat(w["start"].replace("Z", "+00:00")).astimezone(TZ)
            e = datetime.fromisoformat(w["end"].replace("Z", "+00:00")).astimezone(TZ) if w.get("end") else None
            strain = (w.get("score") or {}).get("strain")
            out.append(f"{w.get('sport_name', 'workout')} {s.strftime('%-I:%M %p')}–{e.strftime('%-I:%M %p') if e else '?'}"
                       + (f" strain {strain:.1f}" if strain else ""))
        return "; ".join(out) or "none recorded"
    except (sqlite3.Error, KeyError, ValueError):
        return "unavailable"


def context_block(rows, s):
    arrow = f" {s['arrow']}" if s.get("arrow") else ""
    slope = "" if s["slope"] is None else f", changing {s['slope']:+.1f} mg/dL per min, projected ~{s['proj20']} in 20 min"
    return "\n".join([
        f"Glucose now: {s['v']} mg/dL{arrow} ({s['trend']}) at {s['at']} PT, {s['age_min']} min ago{slope}.",
        f"Last 3 hours, every 15 min: {series(rows)}",
        f"Today {today_summary()}.",
        f"Insulin on board: NOT visible live. Omnipod data only reaches Glooko hours later. Ask David to read IOB off the Omnipod 5 app before any dose suggestion.",
        f"Latest pump boluses on record (from Glooko, may be hours old): {last_boluses()}.",
        f"WHOOP workouts since 6 PM yesterday (WHOOP synced at {whoop_synced_at()}): {workouts_today()}.",
    ])


def whoop_synced_at():
    try:
        return datetime.fromtimestamp(WHOOP_DB.stat().st_mtime, TZ).strftime("%-I:%M %p")
    except OSError:
        return "unknown"
