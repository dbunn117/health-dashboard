#!/usr/bin/env python3
"""Build portal_data.json for the Health Portal from the Health OS sqlite + local JSON sources.

Read-only against sources. Output is a single compact JSON the portal page loads.
"""
import base64, csv, json, math, re, sqlite3, struct, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

DB = Path("/root/health-dashboard/data/health.sqlite")
DASH = Path("/root/health-dashboard/data/dashboard_data.json")
DEXA = Path("/root/health-data/dexa/dexa_measurements.json")
LADDER_DIR = Path("/root/health-data/ladder")
FOOD_LOG = Path("/root/obsidian/David OS/03 Health/Training & Diet/Daily Food Log.md")
HEATH_NOTE = Path("/root/health-portal/heath_note.json")
TZ = ZoneInfo("America/Los_Angeles")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/health-portal/portal_data.json")

EXTRA_DEXA = []
# Labs not in the Sutter export (from vault: Function Health Lab Results - Sep 2026).
EXTRA_A1C = [{"date": "2026-08-20", "value": 7.3, "source": "Function Health (Quest)"}]

TARGETS = {
    "tir": {"green": 85, "target": 75, "warning": 65},
    "lowMax": 1.0, "veryLowMax": 0.1, "sdMax": 30, "a1cMax": 7.0,
    "sleepHours": [7, 8], "strengthPerWeek": 4, "proteinG": [175, 195],
    "hrvBaseline": 52, "rhrBaseline": 50,
    # Omnipod 5 bolus settings (vault: 03 Health/Omnipod 5 Settings.md). Update both when the pump changes.
    "icr": 10, "isf": 30,
}

def r(x, n=1):
    return None if x is None else round(float(x), n)

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# --- CGM: per-day 288 x 5-minute slots, uint16, base64 ---
slots = defaultdict(lambda: [0] * 288)
for row in c.execute("select ts, local_date, glucose from cgm_readings where glucose is not null"):
    ts = row["ts"]
    try:
        hh, mm = int(ts[11:13]), int(ts[14:16])
    except ValueError:
        continue
    slots[row["local_date"]][(hh * 60 + mm) // 5] = int(round(row["glucose"]))
cgm = {d: base64.b64encode(struct.pack("<288H", *v)).decode() for d, v in slots.items()}

def day_sd(vals):
    v = [x for x in vals if x]
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))

carbs = {row[0]: row[1] for row in c.execute(
    "select local_date, sum(coalesce(carbs,0)) from boluses group by local_date")}

days = []
for row in c.execute("select * from daily_summary order by local_date"):
    d = row["local_date"]
    days.append([d, row["cgm_count"], r(row["avg_glucose"]), r(day_sd(slots[d]) if d in slots else None),
                 r(row["tir_70_180"], 2), r(row["very_low_pct"], 2), r(row["low_pct"], 2),
                 r(row["high_pct"], 2), r(row["very_high_pct"], 2),
                 r(row["total_insulin"]), r(row["total_basal"]), r(row["total_bolus"]),
                 r(carbs.get(d), 0)])

def rows(sql):
    return [dict(x) for x in c.execute(sql)]

recovery = rows("select local_date d, recovery_score rec, hrv, rhr from whoop_recovery order by local_date")
sleep = rows("select local_date d, sleep_performance perf, asleep_hours hrs, in_bed_hours bed, deep_sleep_hours deep, "
             "rem_sleep_hours rem, disturbances dist, sleep_consistency cons from whoop_sleep order by local_date")
workouts = rows("select local_date d, sport_name sport, start, strain, avg_hr, max_hr, zone2_min z2, "
                "coalesce(zone3_min,0)+coalesce(zone4_min,0)+coalesce(zone5_min,0) z345 from whoop_workouts order by start")
for coll in (recovery, sleep, workouts):
    for x in coll:
        for k, v in list(x.items()):
            if isinstance(v, float):
                x[k] = round(v, 2)

dexa = []
for s in json.loads(DEXA.read_text()):
    dexa.append({"date": s["date"], "bf": s.get("body_fat_pct"), "total": s.get("total_mass_lb"),
                 "fat": s.get("fat_tissue_lb"), "lean": s.get("lean_tissue_lb"), "bmc": s.get("bmc_lb"),
                 "rmr": s.get("rmr_cal_day"), "vat": s.get("vat_mass_lb"), "ag": s.get("a_g_ratio"),
                 "source": "BodySpec PDF"})
have = {s["date"] for s in dexa}
dexa += [s for s in EXTRA_DEXA if s["date"] not in have]
dexa.sort(key=lambda s: s["date"])

dash = json.loads(DASH.read_text())
a1c = []
for x in (dash.get("sutter") or {}).get("a1c_history") or []:
    m, d, y = x["date"].split("/")
    a1c.append({"date": f"{y}-{m}-{d}", "value": x["value"], "source": "Sutter"})
a1c += EXTRA_A1C
a1c.sort(key=lambda x: x["date"])

def num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def lift_name(n):
    n = re.sub(r"\s*•\s*(Left|Right|L|R)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


sessions, lifts = {}, {}
hist = sorted(LADDER_DIR.glob("workout_history_*.csv"))
jour = sorted(LADDER_DIR.glob("workout_journal_*.csv"))
if hist:
    for r in csv.DictReader(open(hist[-1], encoding="utf-8-sig")):
        if str(r.get("wo_session_complete")).lower() != "true":
            continue
        m, d, y = r["wo_start_date"].split("/")
        ds = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        sessions.setdefault(ds, []).append([r.get("workout_type", "").title(), num(r.get("wo_duration_mins")), r.get("workout_name", "").strip()])
per = {}
if jour:
    for r in csv.DictReader(open(jour[-1], encoding="utf-8-sig")):
        w, reps = num(r.get("mass_value")), num(r.get("logged_reps"))
        if not w or not reps or (r.get("mass_unit") or "").upper() not in ("POUND", ""):
            continue
        ts = datetime.strptime(r["journal_log_time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        k = (lift_name(r["movement_name"]), ts.date().isoformat())
        e = w * (1 + reps / 30) if reps <= 15 else None
        cur = per.setdefault(k, [0.0, 0.0, 0])
        cur[0] = max(cur[0], e or 0); cur[1] += num(r.get("logged_volume")) or w * reps; cur[2] += 1
export_end = max(sessions) if sessions else "0000"
# sessions after the export come from the screen-recording pipeline (ladder-workout-video-ingest)
for w in (dash.get("ladder_exercises") or {}).get("workout_details") or []:
    if w["date"] <= export_end:
        continue
    sessions.setdefault(w["date"], []).append(["Strength", None, (w.get("title") or "").strip()])
    for e in w.get("exercises", []):
        if e.get("weight_lb") and e.get("reps"):
            k = (lift_name(e["name"]), w["date"]); cur = per.setdefault(k, [0.0, 0.0, 0])
            cur[0] = max(cur[0], e["weight_lb"] * (1 + e["reps"] / 30)); cur[1] += e["weight_lb"] * e["reps"]; cur[2] += 1
for (name, d), (e1rm, vol, nsets) in per.items():
    lifts.setdefault(name, []).append([d, round(e1rm, 1) if e1rm else None, round(vol), nsets])
for v in lifts.values():
    v.sort()
day_volume = {}
for (name, d), (_, vol, _) in per.items():
    day_volume[d] = day_volume.get(d, 0) + vol
ladder_out = {"export_through": export_end, "sessions": [[d, *s] for d in sorted(sessions) for s in sessions[d]],
              "volume": [[d, round(v)] for d, v in sorted(day_volume.items())],
              "lifts": {k: v for k, v in lifts.items() if len(v) >= 3}}

# food log (Heath writes it; see health-nutrition-macro-tracking skill)
food = {}
if FOOD_LOG.exists():
    cur_date, cols = None, None
    for line in FOOD_LOG.read_text(errors="ignore").splitlines():
        m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", line)
        if m:
            cur_date, cols = m[1], None
            continue
        if line.startswith("## "):
            cur_date = None
            continue
        if not cur_date or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cols is None:
            if any("Meal" in c for c in cells):
                cols = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        def val(i):
            if i >= len(cells):
                return None
            m2 = re.search(r"-?\d+(?:\.\d+)?", cells[i].replace(",", ""))
            return float(m2[0]) if m2 else None
        kcal, prot, carb, fat = val(2), val(3), val(4), val(5)
        if kcal is None and prot is None:
            continue
        f = food.setdefault(cur_date, {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0, "meals": 0})
        f["kcal"] += kcal or 0; f["protein"] += prot or 0; f["carbs"] += carb or 0; f["fat"] += fat or 0; f["meals"] += 1
food_out = [[d, round(v["kcal"]), round(v["protein"]), round(v["carbs"]), round(v["fat"]), v["meals"]] for d, v in sorted(food.items())]

# pump boluses for the meal-outcome tables: [local date, minute of day, units, carbs]
boluses = []
for row in c.execute("select ts, insulin_delivered, carbs from boluses order by ts"):
    try:
        t = datetime.fromisoformat(row[0]).astimezone(TZ)
    except (TypeError, ValueError):
        continue
    boluses.append([t.date().isoformat(), t.hour * 60 + t.minute, round(float(row[1] or 0), 2), round(float(row[2] or 0))])

# training sessions with exact times (WHOOP start/end, Ladder session type), shared with settings_analysis.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings_analysis import training_sessions
sessions = []
for x in training_sessions(c, "2024-01-01"):
    s0, e0 = x["s"].astimezone(TZ), x["e"].astimezone(TZ)
    sessions.append([s0.date().isoformat(), s0.hour * 60 + s0.minute, round((e0 - s0).total_seconds() / 60), x["kind"]])

# training log: every WHOOP workout (calories, strain, HR, zone minutes) with the Ladder session it
# overlaps (type + exercises); Ladder sessions WHOOP missed are added without WHOOP numbers.
WHOOP_HIST = Path("/root/health-data/whoop/whoop_history.sqlite")
LADDER_TYPE = {"LOWER BODY STRENGTH": "Ladder · Lower body (leg day)", "UPPER BODY STRENGTH": "Ladder · Upper body",
               "FULL BODY STRENGTH": "Ladder · Full body", "CONDITIONING": "Ladder · Conditioning"}
WHOOP_NAME = {"hiit": "HIIT", "activity": "General activity", "australian-football": "Footy"}


def ladder_category_from_title(t):
    t = t.lower()
    for keys, cat in ((("leg", "lower"), "LOWER BODY STRENGTH"), (("upper", "push", "pull", "arm", "chest", "back"), "UPPER BODY STRENGTH"),
                      (("full",), "FULL BODY STRENGTH"), (("condition",), "CONDITIONING")):
        if any(k in t for k in keys):
            return LADDER_TYPE[cat]
    return "Ladder · Strength"


def ladder_detail():
    meta = {}
    if hist:
        for row in csv.DictReader(open(hist[-1], encoding="utf-8-sig")):
            if str(row.get("wo_session_complete")).lower() == "true":
                m, d, y = row["wo_start_date"].split("/")
                meta[(f"{int(y):04d}-{int(m):02d}-{int(d):02d}", row["workout_name"].strip())] = (row.get("workout_type", "").upper(), num(row.get("wo_duration_mins")))
    sess = {}
    if jour:
        for row in csv.DictReader(open(jour[-1], encoding="utf-8-sig")):
            if not row.get("journal_log_time_utc"):
                continue
            t = datetime.strptime(row["journal_log_time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
            k = (t.date().isoformat(), row["workout_name"].strip())
            x = sess.setdefault(k, {"end": t, "moves": {}})
            x["end"] = max(x["end"], t)
            mv = x["moves"].setdefault(lift_name(row["movement_name"]), [0, 0, 0, 0, 0])  # sets, best lb, reps at best, max reps, max seconds
            mv[0] += 1
            w, reps, secs = num(row.get("mass_value")) or 0, num(row.get("logged_reps")) or 0, num(row.get("time_in_seconds")) or 0
            if w > mv[1]:
                mv[1], mv[2] = w, reps
            mv[3], mv[4] = max(mv[3], reps), max(mv[4], secs)
    out = []
    for (d, name), x in sess.items():
        typ, dur = meta.get((d, name), ("", None))
        ex = []
        for mv, (n, w, r_, mr, ms) in x["moves"].items():
            sets = f"{n} set{'s' if n != 1 else ''}"
            ex.append([mv, f"{sets}, best {w:g} lb × {r_:g}" if w else f"{sets}, up to {mr:g} reps" if mr else f"{sets} × {ms:g} s" if ms else sets])
        out.append({"date": d, "end": x["end"], "name": name, "cat": LADDER_TYPE.get(typ, "Ladder · Strength"), "dur": dur, "ex": ex})
    for w in (dash.get("ladder_exercises") or {}).get("workout_details") or []:  # screen-recorded sessions after the export
        if w["date"] <= export_end:
            continue
        names = []
        for e in w.get("exercises", []):
            nm = lift_name(e.get("name") or "")
            if nm and (not names or names[-1] != nm):
                names.append(nm)
        title = (w.get("title") or "").strip()
        out.append({"date": w["date"], "end": None, "name": title, "cat": ladder_category_from_title(title), "dur": None, "ex": [[n, ""] for n in names]})
    return out


training_log = []
if WHOOP_HIST.exists():
    lad = ladder_detail()
    used = set()
    wc = sqlite3.connect(WHOOP_HIST)
    for (js,) in wc.execute("SELECT json FROM workout ORDER BY t"):
        w = json.loads(js)
        try:
            s0 = datetime.fromisoformat(w["start"].replace("Z", "+00:00")).astimezone(TZ)
            e0 = datetime.fromisoformat(w["end"].replace("Z", "+00:00")).astimezone(TZ)
        except (KeyError, ValueError, AttributeError):
            continue
        sc = w.get("score") or {}
        zd = sc.get("zone_durations") or {}
        zones = [round((zd.get(f"zone_{k}_milli") or 0) / 60000, 1) for k in ("zero", "one", "two", "three", "four", "five")]
        sport = (w.get("sport_name") or "workout").lower()
        hit = None
        for i, l in enumerate(lad):
            if i in used:
                continue
            if l["end"] is not None and s0 - timedelta(minutes=20) <= l["end"] <= e0 + timedelta(minutes=30):
                hit = i; break
            if l["end"] is None and l["date"] == s0.date().isoformat() and sport in ("hiit", "weightlifting", "functional-fitness", "powerlifting"):
                hit = i; break
        entry = {"d": s0.date().isoformat(), "m": s0.hour * 60 + s0.minute, "dur": round((e0 - s0).total_seconds() / 60),
                 "cat": WHOOP_NAME.get(sport, sport.replace("-", " ").title()), "name": "", "strain": round(sc.get("strain") or 0, 1),
                 "kcal": round((sc.get("kilojoule") or 0) / 4.184), "hr": sc.get("average_heart_rate"), "maxhr": sc.get("max_heart_rate"),
                 "z": zones, "ex": []}
        if hit is not None:
            used.add(hit); l = lad[hit]
            entry.update(cat=l["cat"], name=l["name"], ex=l["ex"])
        training_log.append(entry)
    for i, l in enumerate(lad):
        if i not in used:
            e0 = l["end"]
            dur = round(l["dur"] or 0)
            start = (e0 - timedelta(minutes=dur)) if e0 else None
            training_log.append({"d": l["date"], "m": start.hour * 60 + start.minute if start else None, "dur": dur or None, "cat": l["cat"], "name": l["name"],
                                 "strain": None, "kcal": None, "hr": None, "maxhr": None, "z": None, "ex": l["ex"]})
    training_log.sort(key=lambda x: (x["d"], x["m"] if x["m"] is not None else 0))

# Sutter visit history from the After Visit Summary export (pipeline/sutter_avs_parse.py).
# Public page, so only structured facts go out: no free-text clinician instructions, no IDs, no pharmacy details.
SUTTER_VISITS = Path("/root/health-data/sutter/avs/visits.json")
SHORT_TEST = [("GLYCOHEMOGLOBIN A1C", "A1c"), ("GLYCOSYLATED HEMOGLOBIN", "A1c"), ("COMPREHENSIVE METABOLIC", "Metabolic panel"), ("BASIC METABOLIC", "Basic metabolic panel"),
              ("LIPID PROFILE", "Lipids"), ("MICROALBUMIN", "Urine microalbumin"), ("THYROID SCREEN", "Thyroid (TSH)"), ("CREATININE", "Creatinine"),
              ("POTASSIUM", "Potassium"), ("ALT,", "ALT (liver)"), ("AST,", "AST (liver)"), ("CONT GLUCOSE MON", "CGM data review")]


def short_test(t):
    u = t.upper()
    for k, v in SHORT_TEST:
        if k in u:
            return v
    return re.sub(r"^PR\s+", "", re.sub(r"\s+for\s+.*$", "", t).strip()).title()


visits = []
if SUTTER_VISITS.exists():
    for v in json.loads(SUTTER_VISITS.read_text()):
        fu = None
        m = re.search(r"around (\d{1,2})/(\d{1,2})/(\d{4})", v.get("follow_up") or "")
        if m:
            fu = f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
        tests = [short_test(x) for x in (v.get("orders") or [])]
        done = [short_test(x) for x in (v.get("procedures") or []) if not re.match(r"^(XR|CT|MRI|US) ", x)]
        img = [re.sub(r"\s+for\s+.*$", "", x).title().replace("Xr ", "X-ray ") for x in (v.get("imaging") or [])]
        started = [re.sub(r"\s*[—(].*$", "", c).strip() for c in (v.get("med_changes") or []) if c and not re.match(r"^(Started|Stopped|Changed) by", c)]
        visits.append({"d": v["date"], "time": v.get("time"), "type": v["type"], "clinician": v.get("clinician"), "place": v.get("place"),
                       "issues": v.get("issues") or [], "vitals": v.get("vitals") or {}, "ordered": sorted(set(tests), key=tests.index),
                       "done": sorted(set(done), key=done.index), "imaging": sorted(set(img), key=img.index), "refilled": v.get("refilled") or [],
                       "meds": sorted(set(started), key=started.index)[:8], "follow_up": fu})

heath = None
if HEATH_NOTE.exists():
    try:
        heath = json.loads(HEATH_NOTE.read_text())
    except ValueError:
        heath = None

latest_ts = c.execute("select max(ts) from cgm_readings").fetchone()[0]
out = {
    "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    "glucose_through": latest_ts,
    "whoop_through": max((x["d"] for x in recovery), default=None),
    "targets": TARGETS,
    "day_fields": ["date", "n", "avg", "sd", "tir", "vlow", "low", "high", "vhigh", "ins", "basal", "bolus", "carbs"],
    "days": days,
    "cgm": cgm,
    "recovery": recovery, "sleep": sleep, "workouts": workouts,
    "dexa": dexa, "a1c": a1c, "ladder": ladder_out, "food": food_out, "heath": heath,
    "boluses": boluses, "sessions": sessions, "training_log": training_log, "visits": visits,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(out, separators=(",", ":")))
print(f"wrote {OUT} {OUT.stat().st_size/1e6:.2f} MB, {len(days)} days, {len(cgm)} cgm days, "
      f"{len(recovery)} recovery, {len(workouts)} workouts, {len(dexa)} dexa, {len(a1c)} a1c, {len(ladder_out['sessions'])} ladder sessions, {len(ladder_out['lifts'])} lifts, {len(food_out)} food days")
