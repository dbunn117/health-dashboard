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


def build_json(conn: sqlite3.Connection, whoop_errors):
    latest = conn.execute('SELECT MAX(local_date), MIN(local_date), COUNT(*) FROM daily_summary').fetchone()
    max_date = latest[0]
    start_30 = (date.fromisoformat(max_date) - timedelta(days=29)).isoformat() if max_date else None
    start_90 = (date.fromisoformat(max_date) - timedelta(days=89)).isoformat() if max_date else None
    daily90 = [round_dict(r) for r in rows(conn, 'SELECT * FROM daily_summary WHERE local_date>=? ORDER BY local_date', (start_90,))]
    daily30 = [r for r in daily90 if r['local_date'] >= start_30]
    whoop30 = [round_dict(r) for r in rows(conn, '''SELECT ds.local_date, wr.recovery_score, wr.hrv, wr.rhr, ws.sleep_performance, ws.sleep_efficiency, ws.asleep_hours,
        ds.avg_glucose, ds.tir_70_180, ds.total_insulin, ds.carbs_sum
        FROM daily_summary ds LEFT JOIN whoop_recovery wr USING(local_date) LEFT JOIN whoop_sleep ws USING(local_date)
        WHERE ds.local_date>=? ORDER BY ds.local_date''', (start_30,))]
    workouts30 = [round_dict(r) for r in rows(conn, 'SELECT * FROM whoop_workouts WHERE local_date>=? ORDER BY local_date DESC, start DESC', (start_30,))]
    hr_zones = {f'zone{i}_min': 0 for i in range(6)}
    for w in workouts30:
        for i in range(6): hr_zones[f'zone{i}_min'] += w.get(f'zone{i}_min') or 0
    hr_zones = {k: round(v,1) for k,v in hr_zones.items()}
    def avg_key(items,k):
        vals=[x[k] for x in items if x.get(k) is not None]
        return round(statistics.mean(vals),2) if vals else None
    summary30={
        'avg_glucose': avg_key(daily30,'avg_glucose'), 'tir_70_180': avg_key(daily30,'tir_70_180'), 'high_pct': avg_key(daily30,'high_pct'),
        'very_high_pct': avg_key(daily30,'very_high_pct'), 'low_pct': avg_key(daily30,'low_pct'), 'very_low_pct': avg_key(daily30,'very_low_pct'),
        'gmi': avg_key(daily30,'gmi'), 'avg_total_insulin': avg_key(daily30,'total_insulin'), 'avg_basal': avg_key(daily30,'total_basal'),
        'avg_bolus': avg_key(daily30,'total_bolus'), 'avg_carbs': avg_key(daily30,'carbs_sum'), 'avg_recovery': avg_key(whoop30,'recovery_score'),
        'avg_hrv': avg_key(whoop30,'hrv'), 'avg_rhr': avg_key(whoop30,'rhr'), 'avg_sleep_performance': avg_key(whoop30,'sleep_performance'),
        'workout_count': len(workouts30), 'hr_zones': hr_zones
    }
    return {
        'generated_at': datetime.now(TZ).isoformat(),
        'date_range': {'start': latest[1], 'end': latest[0], 'days': latest[2]},
        'summary30': summary30,
        'daily90': daily90,
        'joined30': whoop30,
        'workouts30': workouts30[:50],
        'whoop_errors': whoop_errors,
        'safety_note': 'Trend summary only — not dosing or treatment advice. Verify against Dexcom/Glooko/Omnipod/WHOOP source apps before making decisions.'
    }


def generate_html(data: dict):
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS/'data.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    html = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>David Health Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0b1020;--card:#141b2d;--muted:#8ea0c1;--text:#f5f7fb;--accent:#70e1b2;--warn:#ffd166;--bad:#ff6b6b;--blue:#7bb7ff;--purple:#c69cff;}
*{box-sizing:border-box} body{margin:0;background:linear-gradient(180deg,#0b1020,#10172a);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;color:var(--text);} 
.wrap{max-width:1080px;margin:0 auto;padding:18px 14px 60px}.hero{padding:18px 4px 12px}.hero h1{font-size:28px;margin:0 0 6px}.sub{color:var(--muted);line-height:1.35}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{background:rgba(20,27,45,.92);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:14px;box-shadow:0 8px 30px rgba(0,0,0,.18)}.metric{font-size:27px;font-weight:750;letter-spacing:-.02em}.label{color:var(--muted);font-size:13px}.good{color:var(--accent)}.warn{color:var(--warn)}.bad{color:var(--bad)}.blue{color:var(--blue)}h2{font-size:19px;margin:6px 0 12px}.wide{grid-column:1/-1}canvas{width:100%;max-height:340px}.pill{display:inline-block;padding:4px 8px;border-radius:999px;background:rgba(255,255,255,.08);color:var(--muted);font-size:12px;margin:2px 4px 2px 0}.table{width:100%;border-collapse:collapse;font-size:13px}.table th,.table td{padding:8px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}.table th{color:var(--muted);font-weight:600}.note{font-size:12px;color:var(--muted);line-height:1.4}.zonebar{display:grid;grid-template-columns:72px 1fr 56px;gap:8px;align-items:center;margin:8px 0}.bar{height:10px;border-radius:10px;background:rgba(255,255,255,.08);overflow:hidden}.fill{height:100%;border-radius:10px;background:linear-gradient(90deg,var(--blue),var(--purple))}@media(max-width:720px){.grid{grid-template-columns:1fr}.metric{font-size:24px}.wrap{padding:14px 10px 48px}.hero h1{font-size:25px}.card{border-radius:16px;padding:12px}}
</style></head><body><div class="wrap">
<section class="hero"><h1>Health Dashboard</h1><div class="sub" id="subtitle"></div><div class="note" id="safety"></div></section>
<section class="grid" id="metrics"></section>
<section class="grid">
  <div class="card wide"><h2>Glucose + WHOOP — last 90 days</h2><canvas id="trend"></canvas></div>
  <div class="card"><h2>Time in glucose ranges — 30 days</h2><canvas id="tir"></canvas></div>
  <div class="card"><h2>Insulin + carbs — last 90 days</h2><canvas id="insulin"></canvas></div>
  <div class="card"><h2>WHOOP recovery / sleep — 30 days</h2><canvas id="whoop"></canvas></div>
  <div class="card"><h2>Workout HR zones — 30 days</h2><div id="zones"></div></div>
  <div class="card wide"><h2>Recent workouts</h2><div id="workouts"></div></div>
</section></div>
<script>
async function main(){
 const data=await fetch('./data.json').then(r=>r.json());
 const s=data.summary30, days=data.daily90, joined=data.joined30;
 document.getElementById('subtitle').textContent=`Generated ${new Date(data.generated_at).toLocaleString()} · Glooko range ${data.date_range.start} → ${data.date_range.end}`;
 document.getElementById('safety').textContent=data.safety_note;
 const metric=(label,val,cls='',suffix='')=>`<div class="card"><div class="label">${label}</div><div class="metric ${cls}">${val??'—'}${val==null?'':suffix}</div></div>`;
 document.getElementById('metrics').innerHTML=[
  metric('30d time in range', s.tir_70_180, s.tir_70_180>=70?'good':s.tir_70_180>=60?'warn':'bad','%'),
  metric('30d avg glucose', s.avg_glucose, '', ' mg/dL'),
  metric('30d GMI', s.gmi, '', '%'),
  metric('Avg total insulin/day', s.avg_total_insulin, 'blue', ' U'),
  metric('Avg recovery', s.avg_recovery, s.avg_recovery>=67?'good':s.avg_recovery>=34?'warn':'bad',''),
  metric('Avg sleep performance', s.avg_sleep_performance, '', '%'),
 ].join('');
 const labels=days.map(d=>d.local_date.slice(5));
 new Chart(document.getElementById('trend'),{type:'line',data:{labels,datasets:[
  {label:'Avg glucose',data:days.map(d=>d.avg_glucose),borderColor:'#7bb7ff',backgroundColor:'#7bb7ff33',yAxisID:'y'},
  {label:'Time in range %',data:days.map(d=>d.tir_70_180),borderColor:'#70e1b2',backgroundColor:'#70e1b233',yAxisID:'y1'},
  {label:'Recovery',data:days.map(d=>{let j=joined.find(x=>x.local_date===d.local_date);return j&&j.recovery_score}),borderColor:'#ffd166',backgroundColor:'#ffd16633',yAxisID:'y1'}
 ]},options:{responsive:true,interaction:{mode:'index',intersect:false},scales:{y:{title:{display:true,text:'mg/dL'},grid:{color:'#ffffff12'}},y1:{position:'right',min:0,max:100,grid:{drawOnChartArea:false}}},plugins:{legend:{labels:{color:'#f5f7fb'}}}}});
 new Chart(document.getElementById('tir'),{type:'doughnut',data:{labels:['Very high','High','Target','Low','Very low'],datasets:[{data:[s.very_high_pct,s.high_pct,s.tir_70_180,s.low_pct,s.very_low_pct],backgroundColor:['#ff6b6b','#ffd166','#70e1b2','#7bb7ff','#c69cff']}]} ,options:{plugins:{legend:{labels:{color:'#f5f7fb'}}}}});
 new Chart(document.getElementById('insulin'),{type:'bar',data:{labels,datasets:[
  {label:'Basal U',data:days.map(d=>d.total_basal),backgroundColor:'#7bb7ff99',stack:'i'},
  {label:'Bolus U',data:days.map(d=>d.total_bolus),backgroundColor:'#c69cff99',stack:'i'},
  {label:'Carbs g',data:days.map(d=>d.carbs_sum),type:'line',borderColor:'#ffd166',yAxisID:'y1'}
 ]},options:{scales:{x:{stacked:true},y:{stacked:true,title:{display:true,text:'Insulin U'}},y1:{position:'right',grid:{drawOnChartArea:false},title:{display:true,text:'Carbs g'}}},plugins:{legend:{labels:{color:'#f5f7fb'}}}}});
 new Chart(document.getElementById('whoop'),{type:'line',data:{labels:joined.map(d=>d.local_date.slice(5)),datasets:[
  {label:'Recovery',data:joined.map(d=>d.recovery_score),borderColor:'#ffd166'},
  {label:'Sleep perf',data:joined.map(d=>d.sleep_performance),borderColor:'#70e1b2'},
  {label:'HRV',data:joined.map(d=>d.hrv),borderColor:'#c69cff'}
 ]},options:{scales:{y:{min:0,max:100}},plugins:{legend:{labels:{color:'#f5f7fb'}}}}});
 const z=s.hr_zones; const max=Math.max(...Object.values(z),1); document.getElementById('zones').innerHTML=Object.entries(z).map(([k,v],i)=>`<div class="zonebar"><div class="label">Zone ${i}</div><div class="bar"><div class="fill" style="width:${100*v/max}%"></div></div><div>${Math.round(v)}m</div></div>`).join('');
 document.getElementById('workouts').innerHTML=`<table class="table"><thead><tr><th>Date</th><th>Sport</th><th>Strain</th><th>Avg/Max HR</th><th>Z3-5</th></tr></thead><tbody>${data.workouts30.slice(0,20).map(w=>`<tr><td>${w.local_date}</td><td>${w.sport_name||''}</td><td>${w.strain??''}</td><td>${w.avg_hr??''}/${w.max_hr??''}</td><td>${Math.round((w.zone3_min||0)+(w.zone4_min||0)+(w.zone5_min||0))}m</td></tr>`).join('')}</tbody></table>`;
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
