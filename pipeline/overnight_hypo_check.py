#!/usr/bin/env python3
"""Check overnight CGM data for hypo events and cross-reference with workout type.

Outputs structured JSON for the cron agent to format.

2026-09-09: this job runs 5 minutes after "Daily Health OS dashboard refresh"
(glooko_export_refresh.py), which already logs into Glooko and pulls a fresh
30-day export. This script used to do its own independent Glooko login and
export on top of that -- two logins to the same account minutes apart for no
reason, doubling MFA/rate-limit exposure. Fixed by reusing that run's export
if it's fresh (see find_recent_export()); only falls back to its own login
if no recent export is found.
"""
import csv
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

PT = timezone(timedelta(hours=-7))
RAW_DIR = Path('/root/health-data/glooko/raw')
LOG_PATH = Path('/root/obsidian/David OS/03 Health/Training & Diet/Ladder Workout Log.md')
PROFILE_ENV = Path(os.environ.get('HERMES_PROFILE_ENV', '/root/.hermes/profiles/heath/.env'))


def load_env():
    env = PROFILE_ENV
    if env.exists():
        for line in env.read_text(errors='ignore').splitlines():
            s = line.strip()
            if s and not s.startswith('#') and '=' in s:
                k, v = s.split('=', 1)
                os.environ.setdefault(k, v)


def find_recent_export(max_age_minutes=120):
    """Reuse the most recent extracted CGM export if it was produced recently
    (i.e. by the 8:00am dashboard refresh job that already ran this morning),
    instead of triggering a second independent Glooko login."""
    if not RAW_DIR.exists():
        return None
    candidates = sorted(RAW_DIR.glob('extracted_*/cgm_data_1.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    newest = candidates[0]
    age_minutes = (datetime.now().timestamp() - newest.stat().st_mtime) / 60
    if age_minutes <= max_age_minutes:
        return newest
    return None


def do_glooko_export():
    """Run a fresh Glooko export and return path to the extracted CGM CSV."""
    load_env()
    email = os.environ.get('GLOOKO_EMAIL')
    password = os.environ.get('GLOOKO_PASSWORD')
    if not email or not password:
        return None

    BASE = 'https://us.my.glooko.com'
    API = 'https://us.api.glooko.com'
    s = requests.Session()
    s.headers.update({'User-Agent': 'Mozilla/5.0 Hermes-Agent/1.0 HealthDashboard'})

    r = s.get(f'{BASE}/users/sign_in?locale=en', timeout=30)
    r.raise_for_status()
    token = re.search(r'name="authenticity_token"\s+value="([^"]+)"', r.text)
    if not token:
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    if not token:
        return None
    token = token.group(1)

    post = s.post(f'{BASE}/users/sign_in?id=login_form&locale=en',
                  data={'authenticity_token': token, 'redirect_to': '', 'language': 'en',
                        'user[email]': email, 'user[password]': password, 'commit': 'Log In'},
                  headers={'Referer': f'{BASE}/users/sign_in?locale=en'},
                  allow_redirects=True, timeout=45)
    post.raise_for_status()
    if '/users/sign_in' in post.url:
        return None

    code = os.environ.get('GLOOKO_CODE')
    m = re.search(r'patient=([a-z]+-[a-z]+-\d+)', post.text)
    if m:
        code = m.group(1)

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=30)
    start_iso = f'{start_date.isoformat()}T00:00:00.000Z'
    end_iso = f'{end_date.isoformat()}T23:59:59.999Z'

    url = f'{API}/api/v3/users/export_csv?glookoCode={quote(code)}&startDate={quote(start_iso)}&endDate={quote(end_iso)}'
    er = s.get(url, headers={'Origin': BASE, 'Referer': f'{BASE}/'},
               timeout=120)
    er.raise_for_status()
    if len(er.content) < 1000 or b'<html' in er.content[:200].lower():
        return None

    zip_path = RAW_DIR / f'glooko_export_{start_date.isoformat()}_to_{end_date.isoformat()}.zip'
    zip_path.write_bytes(er.content)

    dest = RAW_DIR / f'extracted_{start_date.isoformat()}_to_{end_date.isoformat()}'
    if dest.exists():
        for p in sorted(dest.rglob('*'), reverse=True):
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)

    cgm_csv = dest / 'cgm_data_1.csv'
    return cgm_csv if cgm_csv.exists() else None


def load_cgm(csv_path):
    """Load CGM data from the CSV."""
    readings = []
    with open(csv_path) as f:
        next(f)  # header line
        next(f)  # column names
        for row in csv.reader(f):
            if len(row) >= 2:
                try:
                    t_str = row[0]
                    if t_str.endswith('Z'):
                        t_str_clean = t_str.replace('Z', '+00:00')
                    else:
                        # Assume the timestamp is in the export's timezone (US/Pacific = PT)
                        t_str_clean = t_str
                    t = datetime.fromisoformat(t_str_clean)
                    # If no timezone info, assume PT
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=PT)
                    g = float(row[1])
                    if 20 <= g <= 400:
                        readings.append((t, g))
                except (ValueError, IndexError):
                    pass
    return sorted(readings, key=lambda x: x[0])


def get_workout_type_for_date(target_date_str):
    """Read Ladder Workout Log and extract the workout type for a given date."""
    if not LOG_PATH.exists():
        return None
    text = LOG_PATH.read_text()

    # Look for the heading: "2026-08-05 — Coach Brian (Day 2/4 — Leg Day)"
    pattern = rf'##\s+{target_date_str}.*?—.*?\([^)]*\)'
    m = re.search(pattern, text)
    if m:
        heading = m.group(0)
        label_lower = heading.lower()
        if 'leg' in label_lower:
            return 'leg_day'
        if 'upper' in label_lower or 'push' in label_lower or 'pull' in label_lower:
            return 'upper_body'
        if 'core' in label_lower:
            return 'core'
        if 'cardio' in label_lower or 'run' in label_lower or 'sprint' in label_lower:
            return 'cardio'
        if 'rest' in label_lower or 'off' in label_lower:
            return 'rest'
        return 'other'

    # Fallback: just check if the date appears at all in any heading
    fallback = re.search(rf'##\s+{target_date_str}', text)
    if fallback:
        return 'logged'
    return None


def check_overnight_lows(readings):
    """Find low episodes in the 22:00-06:00 overnight window for the most recent night."""
    now = datetime.now(PT)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Define the overnight window: 22:00 today back to 22:00 yesterday
    overnight_start = today - timedelta(hours=2)  # 22:00 yesterday
    overnight_end = today + timedelta(hours=6)     # 06:00 today

    # Filter readings in the overnight window
    window = [(t, g) for t, g in readings if overnight_start <= t <= overnight_end]
    if not window:
        return []

    # Group into episodes
    episodes = []
    cur = None
    prev_ts = None
    for t, g in window:
        if g < 70:
            if cur is None:
                cur = {'start': t, 'end': t, 'readings': [(t, g)], 'nadir': g, 'nadir_ts': t}
            elif prev_ts and (t - prev_ts).total_seconds() > 600:
                episodes.append(cur)
                cur = {'start': t, 'end': t, 'readings': [(t, g)], 'nadir': g, 'nadir_ts': t}
            else:
                cur['end'] = t
                cur['readings'].append((t, g))
                if g < cur['nadir']:
                    cur['nadir'] = g
                    cur['nadir_ts'] = t
        else:
            if cur:
                episodes.append(cur)
                cur = None
        prev_ts = t
    if cur:
        episodes.append(cur)

    for e in episodes:
        e['duration_min'] = int((e['end'] - e['start']).total_seconds() / 60) + 5
        e['very_low'] = e['nadir'] < 54
        e['nadir_str'] = f"{e['nadir']:.0f}"
        e['start_str'] = e['start'].strftime('%H:%M')
        e['end_str'] = e['end'].strftime('%H:%M')

    return episodes


def main():
    csv_path = find_recent_export()
    if not csv_path:
        csv_path = do_glooko_export()
    if not csv_path:
        print(json.dumps({'status': 'export_failed', 'error': 'Could not pull Glooko export'}))
        return 1

    readings = load_cgm(csv_path)
    if not readings:
        print(json.dumps({'status': 'no_data', 'error': 'No CGM readings in export'}))
        return 1

    latest_ts = readings[-1][0]
    episodes = check_overnight_lows(readings)

    # Get yesterday's date
    yesterday = (datetime.now(PT) - timedelta(days=1)).strftime('%Y-%m-%d')
    workout_type = get_workout_type_for_date(yesterday)

    result = {
        'status': 'ok',
        'latest_cgm': latest_ts.isoformat(),
        'check_window': {
            'start': (datetime.now(PT).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=2)).isoformat(),
            'end': (datetime.now(PT).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=6)).isoformat(),
        },
        'yesterday_date': yesterday,
        'yesterday_workout': workout_type or 'unknown',
        'overnight_episodes': [{
            'start': e['start_str'],
            'end': e['end_str'],
            'duration_min': e['duration_min'],
            'nadir': e['nadir_str'],
            'very_low': e['very_low'],
            'readings': [{'t': t.strftime('%H:%M'), 'g': f'{g:.0f}'} for t, g in e['readings']],
        } for e in episodes],
        'episode_count': len(episodes),
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())