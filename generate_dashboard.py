#!/usr/bin/env python3
"""Generate David's mobile health dashboard from Glooko CSV exports + live WHOOP CLI data.

Read-only. Produces:
- data/health.sqlite
- data/dashboard_data.json
- docs/index.html

Safety: observational trend dashboard only; no dosing advice.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/root/health-dashboard')
GLOOKO_RAW = Path('/root/health-data/glooko/raw')
DATA = ROOT / 'data'
DOCS = ROOT / 'docs'
SQLITE = DATA / 'health.sqlite'
JSON_OUT = DATA / 'dashboard_data.json'
TZ = ZoneInfo('America/Los_Angeles')


def parse_dt(s: str) -> datetime | None:
    s = (s or '').strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.astimezone(TZ)
    except Exception:
        return None


def fnum(x, default=None):
    try:
        if x is None or str(x).strip() == '':
            return default
        # Glooko sometimes uses HI/LO in summary views, but CSV values here are numeric.
        return float(x)
    except Exception:
        return default


def read_glooko_csv(path: Path):
    with path.open(encoding='utf-8-sig', errors='replace', newline='') as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    if rows[0] and rows[0][0].startswith('Name:'):
        header = rows[1] if len(rows) > 1 else []
        data = rows[2:]
    else:
        header = rows[0]
        data = rows[1:]
    return header, data


def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.executescript('''
    DROP TABLE IF EXISTS cgm_readings;
    DROP TABLE IF EXISTS bg_readings;
    DROP TABLE IF EXISTS insulin_daily;
    DROP TABLE IF EXISTS boluses;
    DROP TABLE IF EXISTS alarms;
    DROP TABLE IF EXISTS daily_summary;
    DROP TABLE IF EXISTS whoop_recovery;
    DROP TABLE IF EXISTS whoop_sleep;
    DROP TABLE IF EXISTS whoop_workouts;

    CREATE TABLE cgm_readings(ts TEXT PRIMARY KEY, local_date TEXT, glucose REAL, source_file TEXT);
    CREATE TABLE bg_readings(ts TEXT, local_date TEXT, glucose REAL, manual_reading TEXT, source_file TEXT);
    CREATE TABLE insulin_daily(ts TEXT, local_date TEXT, total_bolus REAL, total_insulin REAL, total_basal REAL, source_file TEXT);
    CREATE TABLE boluses(ts TEXT, local_date TEXT, insulin_type TEXT, bg_input REAL, carbs REAL, carb_ratio REAL, insulin_delivered REAL, source_file TEXT);
    CREATE TABLE alarms(ts TEXT, local_date TEXT, event TEXT, source_file TEXT);
    CREATE TABLE daily_summary(local_date TEXT PRIMARY KEY, cgm_count INTEGER, avg_glucose REAL, median_glucose REAL, gmi REAL,
      tir_70_180 REAL, very_high_pct REAL, high_pct REAL, low_pct REAL, very_low_pct REAL,
      total_insulin REAL, total_basal REAL, total_bolus REAL, bolus_sum REAL, carbs_sum REAL, bolus_count INTEGER, alarm_count INTEGER);
    CREATE TABLE whoop_recovery(local_date TEXT PRIMARY KEY, recovery_score REAL, hrv REAL, rhr REAL, spo2 REAL, skin_temp_c REAL);
    CREATE TABLE whoop_sleep(local_date TEXT PRIMARY KEY, sleep_performance REAL, sleep_efficiency REAL, sleep_consistency REAL, asleep_hours REAL, in_bed_hours REAL, disturbances INTEGER);
    CREATE TABLE whoop_workouts(id TEXT PRIMARY KEY, local_date TEXT, sport_name TEXT, start TEXT, end TEXT, strain REAL, avg_hr REAL, max_hr REAL,
      zone0_min REAL, zone1_min REAL, zone2_min REAL, zone3_min REAL, zone4_min REAL, zone5_min REAL);
    ''')
    conn.commit()


def ingest_glooko(conn: sqlite3.Connection):
    cur = conn.cursor()
    chunk_dirs = sorted(GLOOKO_RAW.glob('extracted_*'))
    if not chunk_dirs:
        raise RuntimeError(f'No extracted Glooko data found under {GLOOKO_RAW}')

    for d in chunk_dirs:
        # CGM files can be cgm_data_1.csv, cgm_data_2.csv, ...
        for p in sorted(d.glob('cgm_data_*.csv')):
            header, rows = read_glooko_csv(p)
            idx = {h: i for i, h in enumerate(header)}
            for r in rows:
                if len(r) < 2:
                    continue
                ts = parse_dt(r[idx.get('Timestamp', 0)])
                glucose = fnum(r[idx.get('CGM Glucose Value (mg/dl)', 1)])
                if ts and glucose is not None:
                    cur.execute('INSERT OR REPLACE INTO cgm_readings VALUES (?,?,?,?)', (ts.isoformat(), ts.date().isoformat(), glucose, str(p)))

        for p in sorted(d.glob('bg_data_*.csv')):
            header, rows = read_glooko_csv(p)
            idx = {h: i for i, h in enumerate(header)}
            for r in rows:
                ts = parse_dt(r[idx.get('Timestamp', 0)]) if r else None
                glucose = fnum(r[idx.get('Glucose Value (mg/dl)', 1)]) if len(r) > 1 else None
                manual = r[idx.get('Manual Reading', 2)] if len(r) > 2 else ''
                if ts and glucose is not None:
                    cur.execute('INSERT INTO bg_readings VALUES (?,?,?,?,?)', (ts.isoformat(), ts.date().isoformat(), glucose, manual, str(p)))

        for p in sorted(d.glob('Insulin data/insulin_data_*.csv')):
            header, rows = read_glooko_csv(p)
            idx = {h: i for i, h in enumerate(header)}
            for r in rows:
                ts = parse_dt(r[idx.get('Timestamp', 0)]) if r else None
                if not ts: continue
                cur.execute('INSERT INTO insulin_daily VALUES (?,?,?,?,?,?)', (
                    ts.isoformat(), ts.date().isoformat(),
                    fnum(r[idx.get('Total Bolus (U)', -1)] if idx.get('Total Bolus (U)', -1) < len(r) else None),
                    fnum(r[idx.get('Total Insulin (U)', -1)] if idx.get('Total Insulin (U)', -1) < len(r) else None),
                    fnum(r[idx.get('Total Basal (U)', -1)] if idx.get('Total Basal (U)', -1) < len(r) else None),
                    str(p)
                ))

        for p in sorted(d.glob('Insulin data/bolus_data_*.csv')):
            header, rows = read_glooko_csv(p)
            idx = {h: i for i, h in enumerate(header)}
            for r in rows:
                ts = parse_dt(r[idx.get('Timestamp', 0)]) if r else None
                if not ts: continue
                def val(name):
                    j = idx.get(name, -1)
                    return r[j] if 0 <= j < len(r) else None
                cur.execute('INSERT INTO boluses VALUES (?,?,?,?,?,?,?,?)', (
                    ts.isoformat(), ts.date().isoformat(), val('Insulin Type'), fnum(val('Blood Glucose Input (mg/dl)')),
                    fnum(val('Carbs Input (g)')), fnum(val('Carbs Ratio')), fnum(val('Insulin Delivered (U)')), str(p)
                ))

        for p in sorted(d.glob('alarms_data_*.csv')):
            header, rows = read_glooko_csv(p)
            idx = {h: i for i, h in enumerate(header)}
            for r in rows:
                ts = parse_dt(r[idx.get('Timestamp', 0)]) if r else None
                event = r[idx.get('Alarm/Event', 1)] if len(r) > 1 else ''
                if ts:
                    cur.execute('INSERT INTO alarms VALUES (?,?,?,?)', (ts.isoformat(), ts.date().isoformat(), event, str(p)))
    conn.commit()


def compute_daily(conn: sqlite3.Connection):
    cur = conn.cursor()
    dates = set(r[0] for r in cur.execute('SELECT DISTINCT local_date FROM cgm_readings'))
    dates |= set(r[0] for r in cur.execute('SELECT DISTINCT local_date FROM insulin_daily'))
    dates |= set(r[0] for r in cur.execute('SELECT DISTINCT local_date FROM boluses'))
    for d in sorted(dates):
        vals = [r[0] for r in cur.execute('SELECT glucose FROM cgm_readings WHERE local_date=? ORDER BY ts', (d,))]
        cgm_count = len(vals)
        avg = statistics.mean(vals) if vals else None
        med = statistics.median(vals) if vals else None
        # Approx GMI formula for mg/dL: 3.31 + 0.02392 * mean glucose
        gmi = 3.31 + 0.02392 * avg if avg is not None and cgm_count >= 72 else None
        def pct(pred):
            return 100 * sum(1 for v in vals if pred(v)) / cgm_count if cgm_count else None
        very_high = pct(lambda v: v > 250)
        high = pct(lambda v: 181 <= v <= 250)
        tir = pct(lambda v: 70 <= v <= 180)
        low = pct(lambda v: 54 <= v < 70)
        very_low = pct(lambda v: v < 54)
        # latest daily insulin record for that date
        rec = cur.execute('SELECT total_insulin,total_basal,total_bolus FROM insulin_daily WHERE local_date=? ORDER BY ts DESC LIMIT 1', (d,)).fetchone()
        total_insulin,total_basal,total_bolus = rec if rec else (None,None,None)
        b = cur.execute('SELECT SUM(insulin_delivered), SUM(carbs), COUNT(*) FROM boluses WHERE local_date=?', (d,)).fetchone()
        bolus_sum, carbs_sum, bolus_count = b if b else (None,None,0)
        alarm_count = cur.execute('SELECT COUNT(*) FROM alarms WHERE local_date=?', (d,)).fetchone()[0]
        cur.execute('INSERT OR REPLACE INTO daily_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            d, cgm_count, avg, med, gmi, tir, very_high, high, low, very_low,
            total_insulin, total_basal, total_bolus, bolus_sum, carbs_sum, bolus_count, alarm_count
        ))
    conn.commit()


def run_whoop(cmd: list[str]) -> dict:
    env_path = '/root/.local/bin:/root/.hermes/profiles/personal/home/.local/bin'
    full = ['bash', '-lc', 'export PATH="%s:$PATH"; %s' % (env_path, ' '.join(cmd))]
    cp = subprocess.run(full, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if cp.returncode != 0:
        return {'error': cp.stderr.strip() or cp.stdout.strip(), 'records': []}
    try:
        data = json.loads(cp.stdout)
        res = data.get('results', {})
        return {'records': res.get('records', []) if isinstance(res, dict) else res, 'meta': data.get('meta')}
    except Exception as e:
        return {'error': f'JSON parse failed: {e}', 'records': []}


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def ingest_whoop(conn: sqlite3.Connection, days=120):
    end = datetime.now(TZ)
    start = end - timedelta(days=days)
    start_s = iso_utc(start)
    end_s = iso_utc(end)
    cur = conn.cursor()
    recov = run_whoop(['whoop-pp-cli','recovery','--agent','--start',start_s,'--end',end_s,'--limit','25','--timeout','60s'])
    for r in recov.get('records', []) or []:
        created = parse_dt(r.get('created_at'))
        if not created: continue
        s = r.get('score') or {}
        cur.execute('INSERT OR REPLACE INTO whoop_recovery VALUES (?,?,?,?,?,?)', (
            created.date().isoformat(), s.get('recovery_score'), s.get('hrv_rmssd_milli'), s.get('resting_heart_rate'), s.get('spo2_percentage'), s.get('skin_temp_celsius')
        ))
    sleep = run_whoop(['whoop-pp-cli','activity','get-sleep-collection','--agent','--start',start_s,'--end',end_s,'--limit','25','--timeout','60s'])
    for r in sleep.get('records', []) or []:
        enddt = parse_dt(r.get('end')) or parse_dt(r.get('created_at'))
        if not enddt: continue
        s = r.get('score') or {}
        st = s.get('stage_summary') or {}
        asleep_ms = (st.get('total_light_sleep_time_milli') or 0) + (st.get('total_rem_sleep_time_milli') or 0) + (st.get('total_slow_wave_sleep_time_milli') or 0)
        in_bed_ms = st.get('total_in_bed_time_milli') or 0
        cur.execute('INSERT OR REPLACE INTO whoop_sleep VALUES (?,?,?,?,?,?,?)', (
            enddt.date().isoformat(), s.get('sleep_performance_percentage'), s.get('sleep_efficiency_percentage'), s.get('sleep_consistency_percentage'),
            asleep_ms/3600000 if asleep_ms else None, in_bed_ms/3600000 if in_bed_ms else None, st.get('disturbance_count')
        ))
    workouts = run_whoop(['whoop-pp-cli','activity','get-workout-collection','--agent','--start',start_s,'--end',end_s,'--limit','25','--timeout','60s'])
    for r in workouts.get('records', []) or []:
        startdt = parse_dt(r.get('start'))
        if not startdt: continue
        s = r.get('score') or {}
        z = s.get('zone_durations') or {}
        cur.execute('INSERT OR REPLACE INTO whoop_workouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            r.get('id'), startdt.date().isoformat(), r.get('sport_name'), r.get('start'), r.get('end'), s.get('strain'), s.get('average_heart_rate'), s.get('max_heart_rate'),
            (z.get('zone_zero_milli') or 0)/60000, (z.get('zone_one_milli') or 0)/60000, (z.get('zone_two_milli') or 0)/60000,
            (z.get('zone_three_milli') or 0)/60000, (z.get('zone_four_milli') or 0)/60000, (z.get('zone_five_milli') or 0)/60000
        ))
    conn.commit()
    return {'recovery_error': recov.get('error'), 'sleep_error': sleep.get('error'), 'workout_error': workouts.get('error')}


def rows(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args)]


def round_dict(d):
    out={}
    for k,v in d.items():
        if isinstance(v,float): out[k]=round(v,2)
        else: out[k]=v
    return out



def clean_float(x, nd=2):
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return round(float(x), nd)
    except Exception:
        return x


def pearson(xs, ys):
    pairs=[(float(x),float(y)) for x,y in zip(xs,ys) if x is not None and y is not None]
    n=len(pairs)
    if n < 8:
        return None
    ax=sum(x for x,_ in pairs)/n; ay=sum(y for _,y in pairs)/n
    num=sum((x-ax)*(y-ay) for x,y in pairs)
    denx=math.sqrt(sum((x-ax)**2 for x,_ in pairs)); deny=math.sqrt(sum((y-ay)**2 for _,y in pairs))
    if denx == 0 or deny == 0:
        return None
    return {'r': round(num/(denx*deny), 2), 'n': n}


def avg_vals(vals):
    vals=[v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def summarize_period(items):
    return {
        'days': len(items),
        'avg_glucose': clean_float(avg_vals([x.get('avg_glucose') for x in items])),
        'tir_70_180': clean_float(avg_vals([x.get('tir_70_180') for x in items])),
        'high_pct': clean_float(avg_vals([x.get('high_pct') for x in items])),
        'very_high_pct': clean_float(avg_vals([x.get('very_high_pct') for x in items])),
        'low_pct': clean_float(avg_vals([x.get('low_pct') for x in items])),
        'very_low_pct': clean_float(avg_vals([x.get('very_low_pct') for x in items])),
        'gmi': clean_float(avg_vals([x.get('gmi') for x in items])),
        'avg_total_insulin': clean_float(avg_vals([x.get('total_insulin') for x in items])),
        'avg_basal': clean_float(avg_vals([x.get('total_basal') for x in items])),
        'avg_bolus': clean_float(avg_vals([x.get('total_bolus') for x in items])),
        'avg_carbs': clean_float(avg_vals([x.get('carbs_sum') for x in items])),
        'avg_cv_pct': clean_float(avg_vals([x.get('cv_pct') for x in items])),
        'overnight_low_pct': clean_float(avg_vals([x.get('overnight_low_pct') for x in items])),
        'evening_avg': clean_float(avg_vals([x.get('evening_avg') for x in items])),
    }


def cgm_day_features(conn, start_date):
    by_day=defaultdict(list)
    by_hour=defaultdict(list)
    for ts_s, d, glucose in conn.execute('SELECT ts, local_date, glucose FROM cgm_readings WHERE local_date>=? ORDER BY ts', (start_date,)):
        dt=parse_dt(ts_s)
        if not dt: continue
        by_day[d].append((dt.hour, glucose))
        by_hour[dt.hour].append(glucose)
    out={}
    for d, vals in by_day.items():
        gl=[v for _,v in vals]
        mean=statistics.mean(gl) if gl else None
        sd=statistics.pstdev(gl) if len(gl)>1 else None
        def segment(pred): return [v for h,v in vals if pred(h)]
        overnight=segment(lambda h: h < 6); morning=segment(lambda h: 6 <= h < 12); afternoon=segment(lambda h: 12 <= h < 18); evening=segment(lambda h: 18 <= h < 24)
        def pctx(xs, pred): return 100*sum(1 for v in xs if pred(v))/len(xs) if xs else None
        out[d]={
            'stddev': clean_float(sd), 'cv_pct': clean_float(100*sd/mean if mean and sd is not None else None),
            'overnight_avg': clean_float(avg_vals(overnight)), 'morning_avg': clean_float(avg_vals(morning)),
            'afternoon_avg': clean_float(avg_vals(afternoon)), 'evening_avg': clean_float(avg_vals(evening)),
            'overnight_low_pct': clean_float(pctx(overnight, lambda v: v < 70)),
            'morning_high_pct': clean_float(pctx(morning, lambda v: v > 180)),
            'evening_high_pct': clean_float(pctx(evening, lambda v: v > 180)),
        }
    hourly=[]
    for h in range(24):
        vals=by_hour.get(h, [])
        hourly.append({'hour': h, 'avg_glucose': clean_float(avg_vals(vals)), 'readings': len(vals)})
    return out, hourly


def build_analytics(conn, daily90, joined30, workouts30, start_30):
    daily_by_date={d['local_date']:d for d in daily90}
    last30=[d for d in daily90 if d['local_date'] >= start_30]
    prev_start=(date.fromisoformat(start_30)-timedelta(days=30)).isoformat()
    prev30=[d for d in daily90 if prev_start <= d['local_date'] < start_30]
    summary7=summarize_period(last30[-7:]); summary14=summarize_period(last30[-14:]); summary30=summarize_period(last30); summary90=summarize_period(daily90); previous30=summarize_period(prev30)
    weekday_groups=defaultdict(list)
    for d in daily90: weekday_groups[date.fromisoformat(d['local_date']).strftime('%a')].append(d)
    weekday_order=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
    weekdays=[{'weekday':w, **summarize_period(weekday_groups.get(w, []))} for w in weekday_order]
    full_days=[d for d in daily90 if d.get('cgm_count',0) >= 200]
    best_tir=sorted(full_days, key=lambda x: (x.get('tir_70_180') or -1), reverse=True)[:5]
    toughest=sorted(full_days, key=lambda x: (x.get('tir_70_180') or 999))[:5]
    high_days=sorted(full_days, key=lambda x: (x.get('avg_glucose') or -1), reverse=True)[:5]
    low_risk=sorted(full_days, key=lambda x: ((x.get('low_pct') or 0)+(x.get('very_low_pct') or 0)), reverse=True)[:5]
    corr_specs=[
        ('Sleep performance vs same-day TIR', joined30, 'sleep_performance', 'tir_70_180', 'Higher sleep performance days tended to align with TIR.'),
        ('Sleep hours vs same-day avg glucose', joined30, 'asleep_hours', 'avg_glucose', 'More sleep vs average glucose.'),
        ('Recovery vs same-day TIR', joined30, 'recovery_score', 'tir_70_180', 'WHOOP recovery and glucose control moved together.'),
        ('HRV vs same-day avg glucose', joined30, 'hrv', 'avg_glucose', 'HRV and average glucose relationship.'),
        ('RHR vs same-day avg glucose', joined30, 'rhr', 'avg_glucose', 'Resting HR and average glucose relationship.'),
        ('Carbs logged vs same-day avg glucose', last30, 'carbs_sum', 'avg_glucose', 'Carb load vs average glucose.'),
        ('Total insulin vs same-day avg glucose', last30, 'total_insulin', 'avg_glucose', 'Total daily insulin vs average glucose.'),
        ('Glucose variability vs TIR', last30, 'cv_pct', 'tir_70_180', 'Variability is usually a major TIR lever.'),
    ]
    correlations=[]
    for label, items, xk, yk, expl in corr_specs:
        res=pearson([i.get(xk) for i in items], [i.get(yk) for i in items])
        if res: correlations.append({'label': label, 'x': xk, 'y': yk, 'r': res['r'], 'n': res['n'], 'note': expl, 'strength': abs(res['r'])})
    correlations=sorted(correlations, key=lambda x: x['strength'], reverse=True)
    workout_by_day=defaultdict(lambda: {'workouts':0,'strain':0,'z3_5_min':0,'z2_min':0,'avg_hr_values':[]})
    for w in workouts30:
        d=w.get('local_date')
        if not d: continue
        g=workout_by_day[d]; g['workouts']+=1; g['strain']+=w.get('strain') or 0
        g['z3_5_min']+=(w.get('zone3_min') or 0)+(w.get('zone4_min') or 0)+(w.get('zone5_min') or 0)
        g['z2_min']+=w.get('zone2_min') or 0
        if w.get('avg_hr') is not None: g['avg_hr_values'].append(w.get('avg_hr'))
    workout_days=[]
    for d,g in sorted(workout_by_day.items()):
        same=daily_by_date.get(d, {}); nd=(date.fromisoformat(d)+timedelta(days=1)).isoformat(); nxt=daily_by_date.get(nd, {})
        workout_days.append({'local_date': d, 'workouts': g['workouts'], 'strain': clean_float(g['strain']), 'z3_5_min': clean_float(g['z3_5_min']), 'z2_min': clean_float(g['z2_min']), 'avg_hr': clean_float(avg_vals(g['avg_hr_values'])), 'same_day_tir': same.get('tir_70_180'), 'same_day_avg_glucose': same.get('avg_glucose'), 'next_day_tir': nxt.get('tir_70_180'), 'next_day_avg_glucose': nxt.get('avg_glucose')})
    workout_corr=[]
    for label, xk, yk in [('Workout Z3-5 min vs same-day TIR','z3_5_min','same_day_tir'),('Workout strain vs next-day avg glucose','strain','next_day_avg_glucose'),('Zone 2 min vs same-day avg glucose','z2_min','same_day_avg_glucose')]:
        res=pearson([i.get(xk) for i in workout_days], [i.get(yk) for i in workout_days])
        if res: workout_corr.append({'label':label,'r':res['r'],'n':res['n']})
    insights=[]
    def add(kind, title, text, stat=None): insights.append({'kind':kind,'title':title,'text':text,'stat':stat})
    if summary30.get('tir_70_180') is not None:
        add('focus' if summary30['tir_70_180'] < 75 else 'win', 'Glucose control baseline', f"Last 30 days: {summary30['tir_70_180']}% time-in-range with average glucose {summary30['avg_glucose']} mg/dL and GMI about {summary30['gmi']}%.", f"{summary30['tir_70_180']}% TIR")
    if summary7.get('tir_70_180') is not None and summary30.get('tir_70_180') is not None:
        delta=summary7['tir_70_180']-summary30['tir_70_180']
        add('win' if delta>=0 else 'watch', 'Recent direction', f"The last 7 days are {abs(delta):.1f} percentage points {'above' if delta>=0 else 'below'} your 30-day TIR baseline.", f"{delta:+.1f} pts")
    if summary30.get('avg_cv_pct') is not None:
        add('focus' if summary30['avg_cv_pct'] > 36 else 'win', 'Stability / variability', f"Average glucose variability is {summary30['avg_cv_pct']}% CV over the last 30 days. Lower variability usually makes gym sessions, work focus, and parenting energy more predictable.", f"CV {summary30['avg_cv_pct']}%")
    if summary30.get('overnight_low_pct') is not None and summary30['overnight_low_pct'] > 1:
        add('watch', 'Overnight lows', f"Overnight readings below 70 averaged {summary30['overnight_low_pct']}%. This is worth reviewing in source apps/with your clinician; the dashboard will only flag trends, not dosing changes.", f"{summary30['overnight_low_pct']}%")
    if summary30.get('evening_avg') is not None and summary30.get('avg_glucose') is not None and summary30['evening_avg'] > summary30['avg_glucose'] + 10:
        add('focus', 'Evening glucose pressure', f"Evening average glucose ({summary30['evening_avg']} mg/dL) is materially higher than the all-day average ({summary30['avg_glucose']} mg/dL), suggesting dinner/evening routine may be a high-leverage place to investigate.", f"{summary30['evening_avg']} mg/dL")
    for c in correlations[:4]:
        direction='positive' if c['r'] > 0 else 'negative'
        add('pattern', c['label'], f"Observed {direction} correlation r={c['r']} across {c['n']} paired days. Treat as a pattern to test, not proof of cause.", f"r={c['r']}")
    if workout_days:
        z35=sum(w.get('z3_5_min') or 0 for w in workout_days)
        add('performance', 'Training load snapshot', f"Across logged workouts in the last 30 days you accumulated about {round(z35)} minutes in HR Zones 3-5. The dashboard now compares hard-training days against same/next-day glucose patterns.", f"{round(z35)}m Z3-5")
    coach_plan=[
        {'title':'Daily readiness check', 'text':'Use recovery + sleep + overnight glucose stability together. Green-light days are good candidates for harder gym work; yellow/red days are better for technique, zone 2, mobility, or simply protecting energy for twins/work.'},
        {'title':'Performance lever to watch', 'text':'Prioritize predictable evenings and overnight stability. A smoother night tends to make the next day’s training, focus, and parenting patience easier.'},
        {'title':'Weekly experiment mindset', 'text':'Pick one hypothesis at a time — timing of dinner, evening walk, workout intensity, bedtime consistency, or hydration — then watch the 7-day vs 30-day cards. Do not use this dashboard to change insulin dosing without your clinical plan.'},
    ]
    return {'periods': {'last7': summary7, 'last14': summary14, 'last30': summary30, 'last90': summary90, 'previous30': previous30}, 'deltas': {k: clean_float(summary30.get(k)-previous30.get(k)) if summary30.get(k) is not None and previous30.get(k) is not None else None for k in ['tir_70_180','avg_glucose','gmi','avg_cv_pct','avg_total_insulin','avg_carbs']}, 'weekdays': weekdays, 'best_days': [round_dict(x) for x in best_tir], 'toughest_days': [round_dict(x) for x in toughest], 'high_days': [round_dict(x) for x in high_days], 'low_risk_days': [round_dict(x) for x in low_risk], 'correlations': correlations, 'workout_days': workout_days, 'workout_correlations': workout_corr, 'insights': insights, 'coach_plan': coach_plan}


def build_json(conn: sqlite3.Connection, whoop_errors):
    latest = conn.execute('SELECT MAX(local_date), MIN(local_date), COUNT(*) FROM daily_summary').fetchone()
    max_date = latest[0]
    start_30 = (date.fromisoformat(max_date) - timedelta(days=29)).isoformat() if max_date else None
    start_90 = (date.fromisoformat(max_date) - timedelta(days=89)).isoformat() if max_date else None
    feature_by_day, hourly = cgm_day_features(conn, start_90)
    base_daily90 = rows(conn, 'SELECT * FROM daily_summary WHERE local_date>=? ORDER BY local_date', (start_90,))
    daily90=[]
    for r in base_daily90:
        rr=round_dict(r); rr.update(feature_by_day.get(rr['local_date'], {})); daily90.append(rr)
    daily30 = [r for r in daily90 if r['local_date'] >= start_30]
    whoop30 = [round_dict(r) for r in rows(conn, '''SELECT ds.local_date, wr.recovery_score, wr.hrv, wr.rhr, ws.sleep_performance, ws.sleep_efficiency, ws.asleep_hours,
        ds.avg_glucose, ds.tir_70_180, ds.total_insulin, ds.carbs_sum
        FROM daily_summary ds LEFT JOIN whoop_recovery wr USING(local_date) LEFT JOIN whoop_sleep ws USING(local_date)
        WHERE ds.local_date>=? ORDER BY ds.local_date''', (start_30,))]
    for r in whoop30: r.update(feature_by_day.get(r['local_date'], {}))
    workouts30 = [round_dict(r) for r in rows(conn, 'SELECT * FROM whoop_workouts WHERE local_date>=? ORDER BY local_date DESC, start DESC', (start_30,))]
    hr_zones = {f'zone{i}_min': 0 for i in range(6)}
    for w in workouts30:
        for i in range(6): hr_zones[f'zone{i}_min'] += w.get(f'zone{i}_min') or 0
    hr_zones = {k: round(v,1) for k,v in hr_zones.items()}
    summary30 = summarize_period(daily30)
    def avg_key(items,k):
        vals=[x[k] for x in items if x.get(k) is not None]
        return round(statistics.mean(vals),2) if vals else None
    summary30.update({'avg_recovery': avg_key(whoop30,'recovery_score'), 'avg_hrv': avg_key(whoop30,'hrv'), 'avg_rhr': avg_key(whoop30,'rhr'), 'avg_sleep_performance': avg_key(whoop30,'sleep_performance'), 'workout_count': len(workouts30), 'hr_zones': hr_zones})
    analytics=build_analytics(conn, daily90, whoop30, workouts30, start_30)
    return {'generated_at': datetime.now(TZ).isoformat(), 'date_range': {'start': latest[1], 'end': latest[0], 'days': latest[2]}, 'summary30': summary30, 'daily90': daily90, 'joined30': whoop30, 'workouts30': workouts30[:50], 'hourly_glucose': hourly, 'analytics': analytics, 'whoop_errors': whoop_errors, 'safety_note': 'Trend summary and coaching context only — not dosing, diagnosis, or treatment advice. Verify against Dexcom/Glooko/Omnipod/WHOOP source apps and your clinical plan before making medical decisions.'}


def generate_html(data: dict):
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS/'data.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    html = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>David Health OS</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#f6f8fb;--surface:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--brand:#4f46e5;--brand2:#06b6d4;--good:#10b981;--warn:#f59e0b;--bad:#ef4444;--purple:#8b5cf6;--shadow:0 18px 50px rgba(15,23,42,.08)}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at 10% -10%,#dbeafe 0,#f8fafc 28%,#f6f8fb 100%);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--ink)}
.app{display:grid;grid-template-columns:250px 1fr;min-height:100vh}.side{padding:24px 18px;border-right:1px solid var(--line);background:rgba(255,255,255,.72);backdrop-filter:blur(18px);position:sticky;top:0;height:100vh}.logo{display:flex;gap:10px;align-items:center;font-weight:850;font-size:19px;letter-spacing:-.03em}.mark{width:36px;height:36px;border-radius:12px;background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 10px 24px #4f46e544}.nav{margin-top:28px;display:grid;gap:8px}.nav a{color:var(--muted);text-decoration:none;padding:10px 12px;border-radius:12px;font-weight:650;font-size:14px}.nav a:hover,.nav a.active{background:#eef2ff;color:var(--brand)}.main{padding:28px;max-width:1280px;width:100%;margin:0 auto;min-width:0}.hero{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr);gap:18px;margin-bottom:18px;min-width:0}.heroCard{border-radius:28px;padding:28px;background:linear-gradient(135deg,#111827,#312e81 60%,#0e7490);color:#fff;box-shadow:var(--shadow);position:relative;overflow:hidden}.heroCard:after{content:"";position:absolute;right:-80px;top:-80px;width:260px;height:260px;background:rgba(255,255,255,.12);border-radius:999px}.eyebrow{font-size:13px;text-transform:uppercase;letter-spacing:.12em;color:#bfdbfe;font-weight:800}.hero h1{font-size:42px;line-height:1.02;margin:10px 0 12px;letter-spacing:-.05em}.hero p{color:#dbeafe;line-height:1.55;max-width:680px}.coach{background:rgba(255,255,255,.9);border:1px solid var(--line);border-radius:28px;padding:22px;box-shadow:var(--shadow)}.coach h2,.section h2{margin:0 0 12px;font-size:20px;letter-spacing:-.02em}.grid{display:grid;gap:16px}.metrics{grid-template-columns:repeat(4,minmax(0,1fr));margin:18px 0}.card{min-width:0;overflow:hidden;background:rgba(255,255,255,.92);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:var(--shadow)}.metricLabel{font-size:13px;color:var(--muted);font-weight:700}.metricVal{font-size:30px;font-weight:850;letter-spacing:-.04em;margin-top:6px}.metricDelta{font-size:12px;margin-top:5px;color:var(--muted)}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.brand{color:var(--brand)}.charts{grid-template-columns:1.4fr .8fr}.two{grid-template-columns:1fr 1fr}.section{margin-top:18px}.insight{display:flex;gap:12px;padding:13px;border:1px solid var(--line);border-radius:16px;margin:10px 0;background:#fff}.dot{width:10px;height:10px;border-radius:99px;margin-top:7px;flex:0 0 auto}.dot.win{background:var(--good)}.dot.focus{background:var(--brand)}.dot.watch{background:var(--warn)}.dot.pattern{background:var(--purple)}.dot.performance{background:var(--brand2)}.insight h3{font-size:14px;margin:0 0 4px}.insight p{font-size:13px;line-height:1.45;margin:0;color:var(--muted)}.pill{display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;background:#f1f5f9;color:#475569;font-weight:750;font-size:12px;margin:3px}.heroCard .pill{background:rgba(255,255,255,.14);color:#fff}.table{width:100%;border-collapse:collapse;font-size:13px}.table th,.table td{padding:10px;border-bottom:1px solid var(--line);text-align:left}.table th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.zonebar{display:grid;grid-template-columns:70px 1fr 54px;gap:8px;align-items:center;margin:10px 0}.bar{height:11px;background:#eef2f7;border-radius:99px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--brand),var(--brand2));border-radius:99px}canvas{width:100%;max-height:360px}.note{font-size:12px;color:#64748b;line-height:1.45}.footer{margin-top:22px;color:var(--muted);font-size:12px}@media(max-width:980px){.app{grid-template-columns:1fr}.side{position:relative;height:auto;border-right:0;border-bottom:1px solid var(--line)}.nav{display:flex;overflow:auto;margin-top:16px}.hero,.charts,.two{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.main{padding:16px}.hero h1{font-size:34px}}@media(max-width:620px){.metrics{grid-template-columns:1fr}.heroCard,.coach,.card{border-radius:18px;padding:16px}.hero h1{font-size:30px}.side{padding:16px}.main{padding:12px}}
</style></head><body><div class="app"><aside class="side"><div class="logo"><div class="mark"></div><span>David Health OS</span></div><nav class="nav"><a class="active" href="#overview">Overview</a><a href="#coach">Coach</a><a href="#patterns">Patterns</a><a href="#training">Training</a><a href="#glucose">Glucose</a></nav><div class="footer" id="meta"></div></aside><main class="main">
<section class="hero" id="overview"><div class="heroCard"><div class="eyebrow">Performance dashboard</div><h1>Train hard. Work clearly. Parent with energy.</h1><p id="heroCopy">Loading...</p><div id="heroPills"></div></div><div class="coach" id="coach"><h2>Coach commentary</h2><div id="coachPlan"></div></div></section>
<section class="grid metrics" id="metrics"></section>
<section class="grid charts section"><div class="card"><h2>90-day control + readiness trend</h2><canvas id="trend"></canvas></div><div class="card"><h2>30-day glucose range mix</h2><canvas id="tir"></canvas><div class="note" id="rangeNote"></div></div></section>
<section class="grid two section" id="patterns"><div class="card"><h2>Patterns Scout found</h2><div id="insights"></div></div><div class="card"><h2>Correlations to test</h2><div id="corrs"></div></div></section>
<section class="grid two section"><div class="card"><h2>Average glucose by hour</h2><canvas id="hourly"></canvas></div><div class="card"><h2>Weekday rhythm</h2><canvas id="weekday"></canvas></div></section>
<section class="grid charts section" id="training"><div class="card"><h2>Training load and glucose</h2><canvas id="trainingChart"></canvas></div><div class="card"><h2>Workout HR zones</h2><div id="zones"></div></div></section>
<section class="grid two section" id="glucose"><div class="card"><h2>Insulin + carbs — 90 days</h2><canvas id="insulin"></canvas></div><div class="card"><h2>Best and toughest days</h2><div id="daysTable"></div></div></section>
<section class="section card"><h2>Recent workouts</h2><div id="workouts"></div></section>
<div class="footer" id="safety"></div></main></div>
<script>
const fmt=(v,d=1)=>v==null?'—':Number(v).toFixed(d).replace(/\.0$/,'');
function chartDefaults(){Chart.defaults.color='#64748b'; Chart.defaults.font.family='Inter, system-ui, sans-serif'; Chart.defaults.plugins.legend.labels.usePointStyle=true;}
function metric(label,val,suffix='',cls='',delta=''){return `<div class="card"><div class="metricLabel">${label}</div><div class="metricVal ${cls}">${val==null?'—':val}${val==null?'':suffix}</div><div class="metricDelta">${delta||'Last 30 days'}</div></div>`}
function insightHtml(i){return `<div class="insight"><div class="dot ${i.kind||'focus'}"></div><div><h3>${i.title} ${i.stat?`<span class="pill">${i.stat}</span>`:''}</h3><p>${i.text}</p></div></div>`}
async function main(){chartDefaults(); const data=await fetch('./data.json').then(r=>r.json()); const s=data.summary30, a=data.analytics, days=data.daily90, joined=data.joined30;
 document.getElementById('meta').innerHTML=`Generated<br>${new Date(data.generated_at).toLocaleString()}<br><br>Data: ${data.date_range.start} → ${data.date_range.end}`;
 document.getElementById('safety').textContent=data.safety_note;
 document.getElementById('heroCopy').textContent=`Current baseline: ${fmt(s.tir_70_180)}% time-in-range, ${fmt(s.avg_glucose)} mg/dL average glucose, ${fmt(s.avg_recovery)} average WHOOP recovery. The aim is practical visibility: what helps you show up strong in the gym, at work, and at home with the twins.`;
 document.getElementById('heroPills').innerHTML=[`GMI ${fmt(s.gmi)}%`,`CV ${fmt(s.avg_cv_pct)}%`,`${s.workout_count} workouts`,`Sleep ${fmt(s.avg_sleep_performance)}%`].map(x=>`<span class="pill">${x}</span>`).join('');
 document.getElementById('coachPlan').innerHTML=a.coach_plan.map(x=>`<div class="insight"><div class="dot focus"></div><div><h3>${x.title}</h3><p>${x.text}</p></div></div>`).join('');
 document.getElementById('metrics').innerHTML=[metric('Time in range',fmt(s.tir_70_180),'%',s.tir_70_180>=70?'good':s.tir_70_180>=60?'warn':'bad',`vs prior 30: ${fmt(a.deltas.tir_70_180)} pts`),metric('Avg glucose',fmt(s.avg_glucose),' mg/dL','brand',`vs prior 30: ${fmt(a.deltas.avg_glucose)} mg/dL`),metric('Glucose variability',fmt(s.avg_cv_pct),'% CV',s.avg_cv_pct<=36?'good':'warn','Lower = more predictable days'),metric('Avg recovery',fmt(s.avg_recovery),'',s.avg_recovery>=67?'good':s.avg_recovery>=34?'warn':'bad','WHOOP 30-day avg'),metric('Sleep performance',fmt(s.avg_sleep_performance),'%','','WHOOP 30-day avg'),metric('Total insulin/day',fmt(s.avg_total_insulin),' U','brand',`basal ${fmt(s.avg_basal)} / bolus ${fmt(s.avg_bolus)}`),metric('Carbs logged/day',fmt(s.avg_carbs),' g','','30-day avg'),metric('Workouts',s.workout_count,'','','Last 30 days')].join('');
 document.getElementById('rangeNote').textContent=`High/very-high together: ${fmt((s.high_pct||0)+(s.very_high_pct||0))}%. Low/very-low together: ${fmt((s.low_pct||0)+(s.very_low_pct||0))}%.`;
 document.getElementById('insights').innerHTML=a.insights.map(insightHtml).join('');
 document.getElementById('corrs').innerHTML=(a.correlations.length?a.correlations:[]).slice(0,8).map(c=>`<div class="insight"><div class="dot pattern"></div><div><h3>${c.label} <span class="pill">r=${c.r}, n=${c.n}</span></h3><p>${c.note} Correlation is observational; use it to choose experiments, not as proof.</p></div></div>`).join('') || '<p class="note">Not enough paired WHOOP + glucose data yet for stable correlations.</p>';
 const labels=days.map(d=>d.local_date.slice(5));
 new Chart(trend,{type:'line',data:{labels,datasets:[{label:'Avg glucose',data:days.map(d=>d.avg_glucose),borderColor:'#4f46e5',backgroundColor:'#4f46e522',yAxisID:'y',tension:.28},{label:'TIR %',data:days.map(d=>d.tir_70_180),borderColor:'#10b981',backgroundColor:'#10b98122',yAxisID:'y1',tension:.28},{label:'Recovery',data:days.map(d=>{let j=joined.find(x=>x.local_date===d.local_date);return j&&j.recovery_score}),borderColor:'#f59e0b',backgroundColor:'#f59e0b22',yAxisID:'y1',tension:.28},{label:'CV %',data:days.map(d=>d.cv_pct),borderColor:'#8b5cf6',backgroundColor:'#8b5cf622',yAxisID:'y1',tension:.28}]},options:{responsive:true,interaction:{mode:'index',intersect:false},scales:{y:{title:{display:true,text:'mg/dL'},grid:{color:'#e2e8f0'}},y1:{position:'right',min:0,max:100,grid:{drawOnChartArea:false}}}}});
 new Chart(tir,{type:'doughnut',data:{labels:['Very high','High','Target','Low','Very low'],datasets:[{data:[s.very_high_pct,s.high_pct,s.tir_70_180,s.low_pct,s.very_low_pct],backgroundColor:['#ef4444','#f59e0b','#10b981','#06b6d4','#8b5cf6'],borderWidth:0}]},options:{cutout:'68%'}});
 new Chart(hourly,{type:'bar',data:{labels:data.hourly_glucose.map(h=>`${h.hour}:00`),datasets:[{label:'Avg glucose',data:data.hourly_glucose.map(h=>h.avg_glucose),backgroundColor:'#4f46e599',borderRadius:7}]},options:{scales:{y:{title:{display:true,text:'mg/dL'}}}}});
 new Chart(weekday,{type:'bar',data:{labels:a.weekdays.map(w=>w.weekday),datasets:[{label:'TIR %',data:a.weekdays.map(w=>w.tir_70_180),backgroundColor:'#10b98199',borderRadius:8},{label:'Avg glucose',data:a.weekdays.map(w=>w.avg_glucose),type:'line',borderColor:'#4f46e5',yAxisID:'y1'}]},options:{scales:{y:{min:0,max:100,title:{display:true,text:'TIR %'}},y1:{position:'right',grid:{drawOnChartArea:false},title:{display:true,text:'mg/dL'}}}}});
 const wd=a.workout_days; new Chart(trainingChart,{type:'bar',data:{labels:wd.map(w=>w.local_date.slice(5)),datasets:[{label:'Z3-5 min',data:wd.map(w=>w.z3_5_min),backgroundColor:'#ef444499',borderRadius:7},{label:'Zone 2 min',data:wd.map(w=>w.z2_min),backgroundColor:'#06b6d499',borderRadius:7},{label:'Same-day TIR',data:wd.map(w=>w.same_day_tir),type:'line',borderColor:'#10b981',yAxisID:'y1'}]},options:{scales:{y:{title:{display:true,text:'minutes'}},y1:{position:'right',min:0,max:100,grid:{drawOnChartArea:false},title:{display:true,text:'TIR %'}}}}});
 new Chart(insulin,{type:'bar',data:{labels,datasets:[{label:'Basal U',data:days.map(d=>d.total_basal),backgroundColor:'#06b6d499',stack:'i',borderRadius:4},{label:'Bolus U',data:days.map(d=>d.total_bolus),backgroundColor:'#8b5cf699',stack:'i',borderRadius:4},{label:'Carbs g',data:days.map(d=>d.carbs_sum),type:'line',borderColor:'#f59e0b',yAxisID:'y1',tension:.25}]},options:{scales:{x:{stacked:true},y:{stacked:true,title:{display:true,text:'Insulin U'}},y1:{position:'right',grid:{drawOnChartArea:false},title:{display:true,text:'Carbs g'}}}}});
 const z=s.hr_zones, max=Math.max(...Object.values(z),1); document.getElementById('zones').innerHTML=Object.entries(z).map(([k,v],i)=>`<div class="zonebar"><div class="metricLabel">Zone ${i}</div><div class="bar"><div class="fill" style="width:${100*v/max}%"></div></div><div>${Math.round(v)}m</div></div>`).join('');
 const rows=(title,arr)=>`<h3 style="font-size:14px;margin:14px 0 6px">${title}</h3><table class="table"><tbody>${arr.slice(0,5).map(d=>`<tr><td>${d.local_date}</td><td>TIR ${fmt(d.tir_70_180)}%</td><td>Avg ${fmt(d.avg_glucose)} mg/dL</td><td>CV ${fmt(d.cv_pct)}%</td></tr>`).join('')}</tbody></table>`;
 document.getElementById('daysTable').innerHTML=rows('Best TIR days',a.best_days)+rows('Toughest TIR days',a.toughest_days);
 document.getElementById('workouts').innerHTML=`<table class="table"><thead><tr><th>Date</th><th>Sport</th><th>Strain</th><th>Avg/Max HR</th><th>Z2</th><th>Z3-5</th></tr></thead><tbody>${data.workouts30.slice(0,24).map(w=>`<tr><td>${w.local_date}</td><td>${w.sport_name||''}</td><td>${fmt(w.strain)}</td><td>${fmt(w.avg_hr,0)}/${fmt(w.max_hr,0)}</td><td>${fmt(w.zone2_min,0)}m</td><td>${fmt((w.zone3_min||0)+(w.zone4_min||0)+(w.zone5_min||0),0)}m</td></tr>`).join('')}</tbody></table>`;
}
main();
</script></body></html>'''
    (DOCS/'index.html').write_text(html, encoding='utf-8')

def main():
    DATA.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    if SQLITE.exists(): SQLITE.unlink()
    conn = sqlite3.connect(SQLITE)
    init_db(conn)
    ingest_glooko(conn)
    compute_daily(conn)
    whoop_errors = ingest_whoop(conn)
    data = build_json(conn, whoop_errors)
    JSON_OUT.write_text(json.dumps(data, indent=2), encoding='utf-8')
    generate_html(data)
    print(json.dumps({
        'sqlite': str(SQLITE),
        'json': str(JSON_OUT),
        'html': str(DOCS/'index.html'),
        'date_range': data['date_range'],
        'summary30': data['summary30'],
        'whoop_errors': whoop_errors,
    }, indent=2))

if __name__ == '__main__':
    main()
