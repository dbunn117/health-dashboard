#!/usr/bin/env python3
"""Live glucose watch: runs every 5 minutes from Heath's Hermes cron (job "Live glucose watch").

Fetches the latest Dexcom Share readings, then applies plain rules. Only when a rule fires does it
print a trigger block for Heath; otherwise the last line is {"wakeAgent": false} and no AI runs.
Every 30 minutes in the daytime it also refreshes today's WHOOP workouts so Heath knows about exercise.

Rules (one per tick, highest first): low (<70), falling (projected <=75 within 20 min), sustained high
(>250 for 2 h), fast rise (+70 in 45 min and above 180), feed down (Dexcom unreachable for 30 min).
Cooldowns, quiet hours (10:30 PM–6:30 AM: low/falling only) and a daily cap keep Heath from nagging.
State: /root/health-data/dexcom/watch_state.json. Thresholds live in RULES below.
"""
import json, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_glucose as L

STATE = L.DIR / "watch_state.json"
RULES = {  # name: (cooldown minutes, allowed in quiet hours, counts toward daily cap)
    "low": (40, True, False),
    "falling": (60, True, False),
    "high": (180, False, True),
    "rising": (120, False, True),
    "feed_down": (360, False, True),
}
DAILY_CAP = 8
QUIET = ((22, 30), (6, 30))


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(st):
    L.DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def quiet(now):
    hm = (now.hour, now.minute)
    return hm >= QUIET[0] or hm < QUIET[1]


def sleep_gate(st):
    save(st)
    print(json.dumps({"wakeAgent": False}))


def maybe_sync_whoop(st, now):
    if quiet(now) or time.time() - st.get("whoop_sync", 0) < 30 * 60:
        return
    st["whoop_sync"] = time.time()
    try:
        import sqlite3
        import whoop_history_sync as W
        start = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cmd, key, tcol = W.KINDS["workout"]
        recs = W.fetch(cmd, start, end)
        con = sqlite3.connect(W.DB)
        con.executemany("INSERT OR REPLACE INTO workout VALUES (?,?,?)",
                        [(str(r.get(key)), r.get(tcol), json.dumps(r, separators=(",", ":"))) for r in recs if r.get(key) is not None])
        con.commit()
    except Exception as e:  # WHOOP is context only; never block the glucose watch
        print(f"whoop workout refresh skipped: {type(e).__name__}", file=sys.stderr)


def evaluate(rows, s):
    v, slope, proj = s["v"], s["slope"], s["proj20"]
    if v < 70:
        return "low", f"below 70: {v} mg/dL"
    falling_arrow = s["trend"] in ("SingleDown", "DoubleDown")
    if v <= 110 and ((proj is not None and proj <= 75 and slope <= -1.5) or (falling_arrow and v <= 100)):
        return "falling", f"heading low: {v} and falling, projected ~{proj} in 20 min"
    two_h = [r for r in rows if r["epoch"] >= s["epoch"] - 120 * 60]
    if len(two_h) >= 20 and all(r["v"] > 250 for r in two_h):
        return "high", f"above 250 for the last 2 hours (now {v})"
    last45 = [r for r in rows if r["epoch"] >= s["epoch"] - 45 * 60]
    if v >= 180 and last45 and v - min(r["v"] for r in last45) >= 70:
        return "rising", f"fast rise: up {v - min(r['v'] for r in last45)} mg/dL in 45 min to {v}"
    return None, None


def main():
    st = load()
    now = datetime.now(L.TZ)
    today = now.strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"], st["count"] = today, 0

    env_stamp = L.ENV.stat().st_mtime if L.ENV.exists() else 0
    if st.get("bad_login") == env_stamp:
        return sleep_gate(st)  # Dexcom rejected the login; wait until .env changes so we can't lock the account
    try:
        L.fetch(minutes=1440 if not st.get("ok_once") else 60)
        st["ok_once"], st["fails"] = True, 0
        st.pop("bad_login", None)
    except L.NoLogin:
        return sleep_gate(st)  # stays silent until David adds his Dexcom login
    except Exception as e:
        if type(e).__name__ == "AccountError":
            st["bad_login"] = env_stamp
            save(st)
            print("TRIGGER: feed_down (Dexcom rejected the saved login, so the live glucose watch has paused). "
                  "Tell David in one sentence to re-run: ssh -t scout-hermes /root/health-dashboard/pipeline/set_dexcom_login.sh")
            print(json.dumps({"wakeAgent": True}))
            return
        st["fails"] = st.get("fails", 0) + 1
        st["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        rule, why = ("feed_down", f"Dexcom Share unreachable for {st['fails'] * 5} min ({type(e).__name__})") if st["fails"] >= 6 else (None, None)
        if not rule:
            return sleep_gate(st)
        rows, s = [], None
    else:
        maybe_sync_whoop(st, now)
        rows = L.recent(240)
        s = L.stats(rows)
        if not s or s["age_min"] > 20:  # sensor warm-up, phone out of range, or upload paused: nothing live to act on
            return sleep_gate(st)
        rule, why = evaluate(rows, s)

    if not rule:
        return sleep_gate(st)
    cooldown, quiet_ok, capped = RULES[rule]
    fired = st.setdefault("fired", {})
    if time.time() - fired.get(rule, 0) < cooldown * 60:
        return sleep_gate(st)
    if rule == "falling" and time.time() - fired.get("low", 0) < 30 * 60:
        return sleep_gate(st)  # already talking about this low
    if quiet(now) and not quiet_ok:
        return sleep_gate(st)
    if capped and st["count"] >= DAILY_CAP:
        return sleep_gate(st)

    fired[rule] = time.time()
    if capped:
        st["count"] += 1
    save(st)
    print(f"TRIGGER: {rule} ({why}). Checked at {now.strftime('%-I:%M %p')} PT. "
          f"Data is from Dexcom Share, usually 5–10 minutes behind the sensor.")
    if s:
        print(L.context_block(rows, s))
    else:
        print(f"Last good reading stored: {L.series(L.recent(60)) or 'none in the last hour'}. Dexcom's own app and alarms are unaffected.")
    print(json.dumps({"wakeAgent": True}))


if __name__ == "__main__":
    main()
