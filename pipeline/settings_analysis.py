#!/usr/bin/env python3
"""How well David's pump settings fit, by time of day and training load. Heath runs this for the
settings-tuning track (t1d-dosing-support skill) and interprets the output.

Method (all from health.sqlite: Glooko boluses + CGM, WHOOP workouts, daily insulin totals):
- Meal events: boluses with >=10 g carbs (boluses within 15 min merged), start glucose 70-180, no other
  bolus in the 2 h before or the 4 h after, CGM coverage >=75% for the 4 h after. Outcome = glucose
  change over 4 h. Implied carb ratio = carbs / (insulin given + change / correction factor): the ratio
  that would have brought glucose back to where it started.
- Corrections: carb-free boluses >=0.5 U at >=150 with nothing else in the 2 h before / 3 h after.
  Observed correction factor = drop over 3 h / units.
- Training: sessions use WHOOP's exact start/end times, labelled with the Ladder session type when a
  Ladder workout overlaps (Ladder logs all sets at the end, so alone it only gives the end time; Ladder
  sessions WHOOP missed use end minus duration). Each meal is tagged by its timing relative to the
  nearest session: before (session starts 0-3 h after the bolus), 0-3 h after, 3-12 h after, next day
  (12-30 h after) or none. Also reported: overnight lows (12-6 AM) after each session type, and daily
  automated basal (Omnipod adds/withholds this itself) as a sensitivity marker.
Automated mode hides the minute-by-minute basal changes, so treat results as direction, not exact values.

Usage: settings_analysis.py [--days 180] [--icr 10] [--isf 30]
"""
import argparse, csv, glob, sqlite3, statistics as stat
from bisect import bisect_left
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")
LADDER_DIR = "/root/health-data/ladder"
WHOOP_KIND = {"weightlifting": "strength", "powerlifting": "strength", "functional-fitness": "strength", "strength-trainer": "strength",
              "crossfit": "strength", "hiit": "conditioning", "pickleball": "pickleball", "tennis": "pickleball",
              "walking": "light", "activity": "light", "yoga": "light", "meditation": "light", "stretching": "light"}
RELS = ["before", "0-3 h after", "3-12 h after", "next day", "none"]


def ladder_sessions():
    """Ladder sessions as (end UTC, type, duration min). End = last set log time; type from the history file."""
    hist = sorted(glob.glob(f"{LADDER_DIR}/workout_history_*.csv"))
    jour = sorted(glob.glob(f"{LADDER_DIR}/workout_journal_*.csv"))
    if not hist or not jour:
        return []
    meta = {}
    for r in csv.DictReader(open(hist[-1], encoding="utf-8-sig")):
        if str(r.get("wo_session_complete")).lower() == "true":
            m, d, y = r["wo_start_date"].split("/")
            meta[(f"{int(y):04d}-{int(m):02d}-{int(d):02d}", r["workout_name"].strip())] = (r.get("workout_type", ""), float(r.get("wo_duration_mins") or 0))
    ends = {}
    for r in csv.DictReader(open(jour[-1], encoding="utf-8-sig")):
        if not r.get("journal_log_time_utc"):
            continue
        t = datetime.strptime(r["journal_log_time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        k = (t.astimezone(TZ).date().isoformat(), r["workout_name"].strip())
        ends[k] = max(ends.get(k, t), t)
    out = []
    for k, e in ends.items():
        typ, dur = meta.get(k, ("", 0))
        out.append((e, typ.upper(), dur or 45))
    return out


def training_sessions(c, since):
    lad = ladder_sessions()
    out = []
    for s, e, strain, sport in c.execute("SELECT start, end, strain, sport_name FROM whoop_workouts WHERE local_date >= date(?, '-2 day')", (since,)):
        try:
            s, e = datetime.fromisoformat(s.replace("Z", "+00:00")), datetime.fromisoformat(e.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        kind = WHOOP_KIND.get((sport or "").lower(), "cardio")
        hit = [x for x in lad if s - timedelta(minutes=20) <= x[0] <= e + timedelta(minutes=30)]
        if hit:
            kind = "conditioning" if "CONDITION" in hit[0][1] else "strength"
        out.append(dict(s=s, e=e, kind=kind, strain=strain or 0, src="WHOOP" + (" + Ladder" if hit else "")))
    for e, typ, dur in lad:
        if e < datetime.fromisoformat(since).replace(tzinfo=TZ) - timedelta(days=2):
            continue
        if not any(x["s"] - timedelta(minutes=20) <= e <= x["e"] + timedelta(minutes=30) for x in out):
            out.append(dict(s=e - timedelta(minutes=dur), e=e, kind="conditioning" if "CONDITION" in typ else "strength", strain=None, src="Ladder only"))
    return sorted(out, key=lambda x: x["s"])

DB = "/root/health-dashboard/data/health.sqlite"
BLOCKS = [("Overnight", 0, 5), ("Breakfast", 5, 10), ("Lunch", 10, 14), ("Afternoon", 14, 17), ("Dinner", 17, 21), ("Late", 21, 24)]


def block(dt):
    return next(n for n, a, b in BLOCKS if a <= dt.hour < b)


def med(xs):
    return stat.median(xs) if xs else None


def iqr(xs):
    if len(xs) < 4:
        return ""
    q = stat.quantiles(xs, n=4)
    return f" (middle half {q[0]:.0f}–{q[2]:.0f})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--icr", type=float, default=10)
    ap.add_argument("--isf", type=float, default=30)
    a = ap.parse_args()
    c = sqlite3.connect(DB)
    since = (datetime.now() - timedelta(days=a.days)).strftime("%Y-%m-%d")

    cg = [(datetime.fromisoformat(t), g) for t, g in c.execute(
        "SELECT ts, glucose FROM cgm_readings WHERE local_date >= date(?, '-1 day') AND glucose IS NOT NULL ORDER BY ts", (since,))]
    ct = [t for t, _ in cg]

    def window(t0, t1):
        i, j = bisect_left(ct, t0), bisect_left(ct, t1)
        return cg[i:j]

    def at(t, tol=10):
        w = window(t - timedelta(minutes=tol), t + timedelta(minutes=tol + 1))
        return min(w, key=lambda x: abs((x[0] - t).total_seconds()))[1] if w else None

    raw = [(datetime.fromisoformat(t), u or 0, cb or 0, bg) for t, u, cb, bg in c.execute(
        "SELECT ts, insulin_delivered, carbs, bg_input FROM boluses WHERE local_date >= ? ORDER BY ts", (since,))]
    ev = []  # merge boluses within 15 min (split doses)
    for t, u, cb, bg in raw:
        if ev and t - ev[-1]["t"] <= timedelta(minutes=15):
            ev[-1]["u"] += u; ev[-1]["carbs"] += cb
        else:
            ev.append(dict(t=t, u=u, carbs=cb, bg=bg))

    sess = training_sessions(c, since)
    real = [x for x in sess if x["kind"] != "light"]

    def relation(t):
        """(timing, session kind) of the session that matters most for a bolus at t."""
        h = lambda td: td.total_seconds() / 3600
        for x in real:
            if 0 <= h(x["s"] - t) <= 3:
                return "before", x["kind"]
        for lo, hi, name in ((-99, 3, "0-3 h after"), (3, 12, "3-12 h after"), (12, 30, "next day")):
            for x in reversed(real):
                d = h(t - x["e"])
                if lo < d <= hi and t >= x["s"]:
                    return name, x["kind"]
        return "none", None

    meals, corrs = [], []
    for i, e in enumerate(ev):
        t = e["t"]
        prev_ok = not any(timedelta(0) < t - x["t"] <= timedelta(hours=2) for x in ev[max(0, i - 6):i])
        if e["carbs"] >= 10:
            nxt_ok = not any(timedelta(0) < x["t"] - t <= timedelta(hours=4) for x in ev[i + 1:i + 8])
            g0 = at(t)
            w = window(t, t + timedelta(hours=4, minutes=1))
            if not (prev_ok and nxt_ok and g0 and 70 <= g0 <= 180 and len(w) >= 36):
                continue
            g4 = at(t + timedelta(hours=4))
            if g4 is None:
                continue
            vals = [g for _, g in w]
            need = e["u"] + (g4 - g0) / a.isf
            first_low = next(((x - t).total_seconds() / 60 for x, g in w if g < 70), None)
            peak_min = (w[vals.index(max(vals))][0] - t).total_seconds() / 60
            meals.append(dict(t=t, block=block(t), carbs=e["carbs"], u=e["u"], d=g4 - g0, peak=max(vals) - g0,
                              first_low=first_low, peak_min=peak_min,
                              low=min(vals) < 70, high_end=g4 > 180, icr=e["carbs"] / need if need > 0.3 else None,
                              rel=relation(t)))
        elif e["u"] >= 0.5 and (e["bg"] or at(t) or 0) >= 150:
            nxt_ok = not any(timedelta(0) < x["t"] - t <= timedelta(hours=3) for x in ev[i + 1:i + 8])
            g0, w = at(t), window(t, t + timedelta(hours=3, minutes=1))
            if not (prev_ok and nxt_ok and g0 and len(w) >= 27):
                continue
            g3 = at(t + timedelta(hours=3))
            if g3 is None:
                continue
            corrs.append(dict(block=block(t), isf=(g0 - g3) / e["u"], low=min(g for _, g in w) < 70, rel=relation(t)))

    print(f"Settings check, last {a.days} days (since {since}). Current settings: carb ratio 1:{a.icr:g}, correction factor {a.isf:g}.")
    print(f"Clean meal boluses: {len(meals)} of {sum(1 for e in ev if e['carbs'] >= 10)}. Clean corrections: {len(corrs)}.\n")

    def meal_line(name, ms, flags=True):
        r = [m["icr"] for m in ms if m["icr"]]
        if len(ms) < 3:
            return f"  {name:<22} n={len(ms):<3} too few clean meals"
        mi = med(r)
        flag = ""
        if flags and len(ms) >= 6 and mi:
            if mi < a.icr * 0.9:
                flag = f"  <- needs MORE insulin than 1:{a.icr:g}"
            elif mi > a.icr * 1.1:
                flag = f"  <- needs LESS insulin than 1:{a.icr:g}"
        return (f"  {name:<22} n={len(ms):<3} 4h change {med([m['d'] for m in ms]):+.0f}{iqr([m['d'] for m in ms])}, "
                f"peak +{med([m['peak'] for m in ms]):.0f}, ended >180 {100 * sum(m['high_end'] for m in ms) / len(ms):.0f}%, "
                f"went <70 {100 * sum(m['low'] for m in ms) / len(ms):.0f}%{low_timing(ms)}, peak at ~{med([m['peak_min'] for m in ms]):.0f} min, "
                f"implied ratio 1:{mi:.1f}{flag}" if mi else
                f"  {name:<22} n={len(ms)} no ratio estimate")

    def low_timing(ms):
        t = [m["first_low"] for m in ms if m["first_low"] is not None]
        return f" (first low ~{med(t):.0f} min after the bolus)" if t else ""

    print("MEALS by time of day (implied ratio = what would have returned glucose to its starting level)")
    for n, _, _ in BLOCKS:
        ms = [m for m in meals if m["block"] == n]
        if ms:
            print(meal_line(n, ms))
    kinds = {}
    for x in real:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    print(f"\nTRAINING SESSIONS in the period: {len(real)} ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))}); "
          f"timed by WHOOP for {sum(1 for x in real if x['src'].startswith('WHOOP'))}, Ladder only for {sum(1 for x in real if x['src'] == 'Ladder only')}.")
    print("\nMEALS by timing relative to training (all times of day mixed; use the per-time-of-day lines below for decisions)")
    for r in RELS:
        ms = [m for m in meals if m["rel"][0] == r]
        if ms:
            print(meal_line({"before": "Before training (0-3 h)"}.get(r, r.capitalize() if r == "none" else r), ms, flags=False))
    print("  Per time of day (implied ratio, n):")
    for n, _, _ in BLOCKS:
        parts = []
        for r in RELS:
            ms = [m for m in meals if m["block"] == n and m["rel"][0] == r and m["icr"]]
            if len(ms) >= 3:
                parts.append(f"{r} 1:{med([m['icr'] for m in ms]):.1f} (n={len(ms)})")
        if len(parts) >= 2:
            print(f"    {n}: " + " | ".join(parts))
    print("  By session type, meals before or within 12 h after:")
    for k in ("strength", "conditioning", "pickleball", "cardio"):
        ms = [m for m in meals if m["rel"][1] == k and m["rel"][0] in ("before", "0-3 h after", "3-12 h after")]
        if len(ms) >= 3:
            print(meal_line(k.capitalize(), ms, flags=False))

    print(f"\nCORRECTIONS (observed drop per unit over 3 h; current factor {a.isf:g})")
    for n, _, _ in BLOCKS:
        cs = [x["isf"] for x in corrs if x["block"] == n]
        if len(cs) >= 3:
            lows = sum(x["low"] for x in corrs if x["block"] == n)
            print(f"  {n:<22} n={len(cs):<3} observed {med(cs):.0f}{iqr(cs)}, went <70 in {lows}")
    allc = [x["isf"] for x in corrs]
    if allc:
        print(f"  {'All':<22} n={len(allc):<3} observed {med(allc):.0f}{iqr(allc)}")

    print("\nNIGHT AFTER (12-6 AM following the day; share of readings below 70, and the lowest reading)")
    kind_by_day = {}
    for x in real:
        kind_by_day.setdefault(x["s"].astimezone(TZ).date().isoformat(), set()).add(x["kind"])
    nights = {}
    for t, g in cg:
        lt = t.astimezone(TZ)
        if lt.hour < 6:
            nights.setdefault((lt.date() - timedelta(days=1)).isoformat(), []).append(g)
    groups = {"strength": [], "conditioning": [], "pickleball": [], "cardio": [], "no training": []}
    for d, vals in nights.items():
        if d < since or len(vals) < 50:
            continue
        ks = kind_by_day.get(d) or {"no training"}
        for k in ks:
            groups.setdefault(k, []).append(vals)
    for k, vs in groups.items():
        if len(vs) >= 3:
            allv = [g for v in vs for g in v]
            print(f"  {k.capitalize():<14} {len(vs):>3} nights, {100 * sum(g < 70 for g in allv) / len(allv):.1f}% below 70, "
                  f"nights with any low {100 * sum(min(v) < 70 for v in vs) / len(vs):.0f}%, median lowest {med([min(v) for v in vs]):.0f}")

    print("\nDAILY AUTOMATED BASAL (Omnipod adds less when you're more insulin-sensitive)")
    days = {d: b for d, b in c.execute("SELECT local_date, max(total_basal) FROM insulin_daily WHERE local_date >= ? GROUP BY local_date", (since,)) if b}
    tdays = set(kind_by_day)
    def avg(ds):
        v = [days[d] for d in ds if d in days]
        return (sum(v) / len(v), len(v)) if v else (None, 0)
    from datetime import date as _d
    after = {(_d.fromisoformat(d) + timedelta(days=1)).isoformat() for d in tdays}
    for label, ds in [("Training days", tdays), ("Day after training", after - tdays), ("Rest days (neither)", set(days) - tdays - after)]:
        m, n = avg(ds)
        if m:
            print(f"  {label:<22} {m:.1f} U basal/day (n={n})")
    print("\nRead this as direction, not exact values: automated mode also adjusts insulin minute by minute, "
          "which this data can't see. Propose one change at a time, about 10%, and re-check after 7–14 days.")


if __name__ == "__main__":
    main()
