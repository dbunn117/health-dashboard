#!/usr/bin/env python3
"""Generate a concise weekly Health OS report from local dashboard JSON.

Read-only trend support. Does not print raw data or credentials.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path('/root/health-dashboard')
DATA_JSON = ROOT / 'data' / 'dashboard_data.json'


def fmt(x, suffix='', digits=1):
    if x is None:
        return 'n/a'
    try:
        if isinstance(x, int) or (isinstance(x, float) and float(x).is_integer()):
            return f'{int(x)}{suffix}'
        return f'{float(x):.{digits}f}{suffix}'
    except Exception:
        return f'{x}{suffix}'


def avg(rows, key):
    # Simple average of values
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else None


def weighted_avg(rows, value_key, weight_key='cgm_count'):
    """Weighted average, weighted by CGM reading count for accuracy."""
    pairs = [(r.get(value_key), r.get(weight_key)) for r in rows
             if isinstance(r.get(value_key), (int, float)) and isinstance(r.get(weight_key), (int, float))]
    total_w = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total_w if total_w else None


def sumv(rows, key):
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    return sum(vals) if vals else None


def main():
    data = json.loads(DATA_JSON.read_text())
    dr = data.get('date_range', {})
    end_s = dr.get('end') or date.today().isoformat()
    end = date.fromisoformat(end_s)
    start = end - timedelta(days=6)
    s30 = data.get('summary30', {})

    daily = [r for r in data.get('daily90', []) if start.isoformat() <= r.get('local_date', '') <= end.isoformat()]
    joined = [r for r in data.get('joined30', []) if start.isoformat() <= r.get('local_date', '') <= end.isoformat()]
    workouts = [r for r in data.get('workouts30', []) if start.isoformat() <= r.get('local_date', '') <= end.isoformat()]

    # Glucose values live most completely in daily90. WHOOP recovery/sleep values live in joined30.
    g_tir = weighted_avg(daily, 'tir_70_180')
    g_avg = avg(daily, 'avg_glucose')
    g_cv = avg(daily, 'cv_pct')
    g_high = avg(daily, 'high_pct')
    g_vhigh = avg(daily, 'very_high_pct')
    g_low = avg(daily, 'low_pct')
    g_vlow = avg(daily, 'very_low_pct')
    insulin = avg(daily, 'total_insulin')
    basal = avg(daily, 'total_basal')
    bolus = avg(daily, 'total_bolus')
    carbs = avg(daily, 'carbs_sum')

    recovery = avg(joined, 'recovery_score')
    hrv = avg(joined, 'hrv')
    rhr = avg(joined, 'rhr')
    sleep_perf = avg(joined, 'sleep_performance')
    asleep = avg(joined, 'asleep_hours')
    workout_count = len(workouts)
    avg_strain = avg(workouts, 'strain')
    total_strain = sumv(workouts, 'strain')

    supply_line = None
    supply_root = data.get('supplies', {})
    supply_items = supply_root.get('supplies') if isinstance(supply_root, dict) else supply_root
    if isinstance(supply_items, list) and supply_items:
        item = supply_items[0]
        if isinstance(item, dict):
            name = item.get('item') or item.get('name') or 'Supply item'
            status = item.get('status') or ''
            next_ready = item.get('next_ready') or item.get('nextReady') or item.get('next_fill') or ''
            supply_line = f'{name}: {status}' + (f' ({next_ready})' if next_ready else '')

    obs = []
    if g_low is not None and g_low >= 2:
        obs.append('Lows were worth watching this week, especially around training/overnight windows if they line up in the detailed view.')
    if g_tir is not None and s30.get('tir_70_180') is not None:
        delta = g_tir - s30['tir_70_180']
        direction = 'above' if delta >= 0 else 'below'
        obs.append(f'7-day TIR was {abs(delta):.1f} pts {direction} the 30-day baseline.')
    if recovery is not None and recovery < 50:
        obs.append('Recovery averaged in the yellow/red zone, so this may be a good week to protect sleep consistency.')
    if workout_count >= 5:
        obs.append(f'Training volume was high at {workout_count} workouts, so recovery/sleep quality may matter more than adding more intensity.')
    if not obs:
        obs.append('No single red-flag pattern jumps out from the weekly summary; keep watching sleep, recovery, training load, and overnight stability together.')

    experiments = [
        'Pick one evening variable to stabilize: dinner timing, late snack, alcohol, or a short post-dinner walk.',
        'Use recovery + overnight glucose stability to choose hard training vs technique/zone 2 days.',
        'Keep one note on any unusual high/low day so the numbers have context next week.',
    ]

    print('**Heath weekly Health OS report**')
    print(f'{start.isoformat()} → {end.isoformat()}')
    print('')
    print(f'**Overall:** 7-day TIR {fmt(g_tir, "%")}, avg glucose {fmt(g_avg, " mg/dL")}, recovery {fmt(recovery)}.')
    print('')
    print('**Glucose + pump context**')
    print(f'- Time in range: {fmt(g_tir, "%")}')
    print(f'- Avg glucose: {fmt(g_avg, " mg/dL")}')
    print(f'- Variability: {fmt(g_cv, "% CV")}')
    print(f'- High / very high: {fmt(g_high, "%")} / {fmt(g_vhigh, "%")}')
    print(f'- Low / very low: {fmt(g_low, "%")} / {fmt(g_vlow, "%")}')
    print(f'- Insulin/day: {fmt(insulin, " U")} (basal {fmt(basal, " U")}, bolus {fmt(bolus, " U")})')
    print(f'- Carbs logged/day: {fmt(carbs, " g")}')
    print('')
    print('**WHOOP**')
    print(f'- Recovery: {fmt(recovery)}')
    print(f'- HRV / RHR: {fmt(hrv, " ms")} / {fmt(rhr, " bpm")}')
    print(f'- Sleep performance / asleep: {fmt(sleep_perf, "%")} / {fmt(asleep, " h")}')
    print(f'- Workouts: {workout_count} sessions, avg strain {fmt(avg_strain)}, total strain {fmt(total_strain)}')
    print('')
    print('**Patterns to watch**')
    for o in obs[:4]:
        print(f'- {o}')
    print('')
    print('**Experiments for next week**')
    for e in experiments:
        print(f'- {e}')
    if supply_line:
        print('')
        print(f'**Supply/admin:** {supply_line}')
    print('')
    print('Safety note: this is trend/context support only, not dosing, diagnosis, or treatment advice. Do not change insulin, pump settings, or medication based on this report without your clinical plan.')


if __name__ == '__main__':
    main()
