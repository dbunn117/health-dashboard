#!/usr/bin/env python3
"""Parse Ladder Workout Log.md → ladder_exercises section in dashboard_data.json.

Extracts structured exercise data so you can query:
  - All sessions for a given exercise (weight progression over time)
  - Total reps/volume of a movement in a date range
  - Exercise frequency, volume trends, and 1RM estimates

Run: python3 extract_exercises.py
(Modifies dashboard_data.json in-place by adding a 'ladder_exercises' key.)
"""

import json, re, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_PATH = Path('/root/obsidian/David OS/03 Health/Ladder Workout Log.md')
DASHBOARD_JSON = Path('/root/health-dashboard/data/dashboard_data.json')

# Bodyweight exercises to skip for weight tracking
BW_EXERCISES = {
    'arm circles', 'forward shoulder roll', 'shoulder roll', 'hamstring sweep',
    'deep squat with t spine rotation', 'rooted balance', 'guided breathwork',
    'dead bug', 'flutter kick', 'leg lift over', 'forrest flow', 'high plank hip tap',
    'push plank to plank jack', 'reverse crunch', 'hollow hold', 'rest + digest',
    'cat cow', 'floating knees', 'tricep pushup', 'pit stop', 'cobra', 'scorpion stretch',
    'swan', 'bear hold leg extension', 'commando to 4 shoulder taps', 'save + recharge',
    'outta control', 'arm swing', 'deep roots', 'purpose complete', 'superhuman',
    'bear to downward dog', 'flow right', 'cobra hold', 'dead bug alternating',
    'plank isometric hold', 'stablize control', 'tempo power mode', 'pushup',
    'the finale', 'flow home', 'stone core', 'back fired glute bridge walkout',
    'return to quad stretch', 'wide leg toe reach static', 'quad stretch',
    'dead bug', 'lift over', 'reverse crunch', 'purpose complete',
}

# Normalize exercise names to canonical forms
EXERCISE_ALIASES = {
    'bicep curl': 'Bicep Curl',
    'bicep curl (alternating)': 'Bicep Curl (Alternating)',
    'bicep curl (back to wall)': 'Bicep Curl (Back to Wall)',
    'hammer curl (alternating)': 'Hammer Curl (Alternating)',
    'hammer curl': 'Hammer Curl',
    'hammer curl to press (kneeling)': 'Hammer Curl to Press (Kneeling)',
    'standing curl press': 'Standing Curl Press',
    'tricep kickback': 'Tricep Kickback',
    'tricep kickback (alt.)': 'Tricep Kickback (Alt.)',
    'tricep dip (elevated)': 'Tricep Dip (Elevated)',
    'tricep dip': 'Tricep Dip (Elevated)',
    'zottman curl': 'Zottman Curl',
    'bent over row': 'Bent Over Row',
    'bent over row 1.5': 'Bent Over Row 1.5',
    'bent over row 1.5 (r)': 'Bent Over Row 1.5 (R)',
    'bent over row 1.5 (l)': 'Bent Over Row 1.5 (L)',
    'bent over row, knee supported (r)': 'Bent Over Row, Knee Supported (R)',
    'bent over row, knee supported (l)': 'Bent Over Row, Knee Supported (L)',
    'gorilla row (alt.)': 'Gorilla Row (Alt.)',
    'paleo pull': 'Gorilla Row (Alt.)',
    'combo pull': 'Bent Over Reverse Fly',
    'bent over reverse fly': 'Bent Over Reverse Fly',
    'rear delt row': 'Rear Delt Row',
    'superhuman combo': 'Rear Delt Row',
    'upright row': 'Upright Row',
    'high voltage': 'Upright Row',
    'summit pull': 'Upright Row',
    'bench press': 'Bench Press',
    'power press': 'Bench Press',
    'sunrise press': 'Incline Bench Press',
    'incline bench': 'Incline Bench Press',
    'shoulder press (seated)': 'Shoulder Press (Seated)',
    'mountain press': 'Shoulder Press',
    'sky builder': 'Shoulder Press (Seated)',
    'chest fly': 'Chest Fly',
    'chest fly (decline floor)': 'Chest Fly (Decline Floor)',
    'expand land': 'Chest Fly (Decline Floor)',
    'open sky': 'Chest Fly',
    'lateral raise': 'Lateral Raise',
    '3 peaks': 'Lateral Raise 3-Step',
    'lateral raise 90°': 'Lateral Raise 90°',
    'power ranger': 'Lateral Raise 90°',
    'front raise': 'Front Raise',
    'front raise (seated alt.)': 'Front Raise (Seated Alt.)',
    'front light': 'Front Raise (Seated Alt.)',
    'halo (kneeling alt.)': 'Halo (Kneeling Alt.)',
    'reactivator': 'Halo (Kneeling Alt.)',
    'deadlift — single leg (r)': 'Deadlift — Single Leg (R)',
    'deadlift — single leg (l)': 'Deadlift — Single Leg (L)',
    'split squat pause (r)': 'Split Squat Pause (R)',
    'split squat pause (l)': 'Split Squat Pause (L)',
    'goblet squat': 'Goblet Squat',
    'goblet reverse lunge to high knee': 'Goblet Reverse Lunge to High Knee',
    'glute bridge': 'Glute Bridge',
    'russian twist': 'Russian Twist',
    'furnace core': 'Russian Twist',
    'romanian deadlift': 'Romanian Deadlift',
    'hinge hq': 'Romanian Deadlift',
    'sumo deadlift': 'Sumo Deadlift',
    'power forge': 'Sumo Deadlift',
    'bulgarian split squat (r)': 'Bulgarian Split Squat (R)',
    'bulgarian split squat (l)': 'Bulgarian Split Squat (L)',
    'unshakable strength': 'Bulgarian Split Squat',
    'swing': 'Swing',
    'power plant': 'Swing',
    'goblet march': 'Goblet March',
    'wild athlete hand to hand swing': 'Wild Athlete Hand to Hand Swing',
    'side plank reach (r)': 'Side Plank Reach (R)',
    'side plank reach (l)': 'Side Plank Reach (L)',
    'overhead tricep extension': 'Overhead Tricep Extension',
    'river lockout': 'Overhead Tricep Extension',
    'bear hold': 'Bear Hold Pull Through',
    'bear hold — pull through': 'Bear Hold Pull Through',
}


def clean_name(name):
    """Normalize an exercise name to its canonical form."""
    n = name.strip().lower()
    n = re.sub(r'[•\*–—]', '', n).strip()
    n = re.sub(r'\s+', ' ', n)
    # Remove leading "Back Boosted — " or similar prefixes
    n = re.sub(r'^.*[—\-–]\s*', '', n)
    # Remove "**" markers
    n = n.replace('**', '')
    if n in EXERCISE_ALIASES:
        return EXERCISE_ALIASES[n]
    # Try to find alias by partial match
    for key, val in EXERCISE_ALIASES.items():
        if key in n or n in key:
            return val
    # Capitalize sensibly
    return ' '.join(w.capitalize() for w in n.split())


def parse_weight_details(details_str):
    """Extract weight and reps from a details string like '80%@25lb → 90%@25lb → 100%@25lb'."""
    sets = []
    # Match patterns like "80%×10@110lb" or "80%@120lb" or "80%×10@25lb" or "AMRAP@25lb"
    parts = re.split(r'→|→', details_str)
    for part in parts:
        part = part.strip()
        # Try: effort%×reps@weight
        m = re.match(r'(\d+)%\s*×\s*(\d+)\s*@\s*(\d+)\s*lb', part)
        if m:
            sets.append({'effort_pct': int(m.group(1)), 'reps': int(m.group(2)), 'weight_lb': int(m.group(3))})
            continue
        # Try: effort%@weight (no reps specified)
        m = re.match(r'(\d+)%\s*@\s*(\d+)\s*lb', part)
        if m:
            sets.append({'effort_pct': int(m.group(1)), 'reps': None, 'weight_lb': int(m.group(2))})
            continue
        # Try: AMRAP@weight
        m = re.match(r'AMRAP\s*@\s*(\d+)\s*lb', part)
        if m:
            sets.append({'effort_pct': None, 'reps': None, 'weight_lb': int(m.group(1))})
            continue
        # Try: just weight with AMRAP: "AMRAP@50lb"
        m = re.match(r'\s*AMRAP\s*@\s*(\d+)', part)
        if m:
            sets.append({'effort_pct': None, 'reps': None, 'weight_lb': int(m.group(1))})
            continue
    return sets


def parse_workout_sections(text):
    """Split workout log into individual workout entries."""
    sections = re.split(r'\n(?=## \d{4}-\d{2}-\d{2})', text)
    return [s for s in sections if s.strip()]


def parse_single_workout(section):
    """Parse a single workout section into structured data."""
    lines = section.split('\n')
    
    # Extract date and title from first line
    header = lines[0].strip()
    m = re.match(r'##\s+(\d{4}-\d{2}-\d{2})\s+.*?(?:—|-)\s*(.*)', header)
    if not m:
        m = re.match(r'##\s+(\d{4}-\d{2}-\d{2})\s*—\s*(.*)', header)
    if not m:
        m = re.match(r'##\s+(\d{4}-\d{2}-\d{2})\s+', header)
    if not m:
        return None
    
    date = m.group(1)
    title = m.group(2).strip() if m.lastindex >= 2 else ''
    
    workout = {
        'date': date,
        'title': title,
        'exercises': [],
        'volume_reps': None,
        'volume_lb': None,
    }
    
    # Extract volume info
    vol_m = re.search(r'\*\*Volume:\*\*\s*([\d,]+)\s*reps\s*[·•]\s*([\d.]+)K\s*lb', section)
    if vol_m:
        workout['volume_reps'] = int(vol_m.group(1).replace(',', ''))
        workout['volume_lb'] = float(vol_m.group(2)) * 1000
    
    # Find the exercise table - look for markdown table after "### Exercises" or similar
    in_table = False
    table_started = False
    table_headers = []
    table_rows = []
    
    for line in lines:
        if re.match(r'###\s+Exercises|###\s+Exercises', line.strip()):
            in_table = True
            table_started = False
            continue
        if in_table:
            if line.strip().startswith('|'):
                cells = [c.strip() for c in line.split('|')]
                cells = [c for c in cells if c]  # Remove empty strings from split
                if not table_started:
                    # Check if this looks like a table header row
                    if all(c.startswith('-') for c in ''.join(cells) if c):
                        table_started = True
                        continue
                    if len(cells) >= 3 and cells[0] in ('Order', 'Order', '#'):
                        table_started = True
                        table_headers = cells
                        continue
                    table_started = True
                    table_headers = cells
                    continue
                else:
                    if len(cells) >= 3 and not all(c.startswith('-') for c in cells):
                        table_rows.append(cells)
            else:
                if table_started and line.strip() == '':
                    break  # End of table
                elif table_started and not line.strip().startswith('|'):
                    # Check if next section
                    if line.strip().startswith('###') or line.strip().startswith('---'):
                        break
    
    # Parse table rows into exercises
    for row in table_rows:
        if len(row) < 3:
            continue
        
        # Try to identify columns
        # Typical: [Order, Exercise, Equipment, Sets, Details]
        # or [Order, Exercise, Equipment, Sets, Weight, Details, ...]
        
        exercise_name = row[1] if len(row) > 1 else ''
        equipment = row[2] if len(row) > 2 else ''
        
        # Skip warmup, cooldown, mobility, breathwork
        skip_keywords = ['warmup', 'cooldown', 'breathwork', 'stretch', 'mobility', 'flow']
        if any(kw in exercise_name.lower() for kw in skip_keywords):
            continue
        
        details = row[-1] if len(row) > 3 else ''
        set_info = row[3] if len(row) > 3 else ''
        
        clean_exercise = clean_name(exercise_name)
        is_bw = equipment.strip().upper() in ('BW', 'BODYWEIGHT', '') or clean_exercise.lower() in BW_EXERCISES
        
        # Parse weight from details
        weight_data = parse_weight_details(details)
        
        # Parse sets count
        sets_count = None
        if set_info:
            sm = re.match(r'(\d+)\s*×', set_info)
            if sm:
                sets_count = int(sm.group(1))
        
        # Extract best weight from weight_data
        best_weight = None
        best_reps = None
        if weight_data:
            for s in weight_data:
                w = s.get('weight_lb')
                if w and (best_weight is None or w > best_weight):
                    best_weight = w
                r = s.get('reps')
                if r and (best_reps is None or r > best_reps):
                    best_reps = r
        
        # Also try to extract weight from exercise name or details directly
        if not best_weight and not is_bw:
            wm = re.search(r'@\s*(\d+)\s*lb', details)
            if wm:
                best_weight = int(wm.group(1))
        
        exercise_entry = {
            'name': clean_exercise,
            'equipment': equipment.strip(),
            'bodyweight': is_bw,
            'sets': sets_count,
            'best_weight_lb': best_weight,
            'best_reps': best_reps,
            'details_raw': details.strip(),
            'weight_data': weight_data,
        }
        
        workout['exercises'].append(exercise_entry)
    
    return workout


def build_exercise_index(workouts):
    """Build index of exercises across all workouts."""
    by_exercise = defaultdict(list)
    all_exercises = set()
    
    for w in workouts:
        if not w:
            continue
        for ex in w['exercises']:
            name = ex['name']
            if name == '' or name == '—':
                continue
            all_exercises.add(name)
            by_exercise[name].append({
                'date': w['date'],
                'workout_title': w['title'],
                'sets': ex['sets'],
                'best_weight_lb': ex['best_weight_lb'],
                'best_reps': ex['best_reps'],
                'bodyweight': ex['bodyweight'],
                'weight_data': ex['weight_data'],
            })
    
    return by_exercise, sorted(all_exercises)


def main():
    if not LOG_PATH.exists():
        print(f"ERROR: {LOG_PATH} not found")
        sys.exit(1)
    if not DASHBOARD_JSON.exists():
        print(f"ERROR: {DASHBOARD_JSON} not found")
        sys.exit(1)
    
    text = LOG_PATH.read_text(encoding='utf-8')
    sections = parse_workout_sections(text)
    
    workouts = []
    for s in sections:
        w = parse_single_workout(s)
        if w:
            workouts.append(w)
    
    by_exercise, exercise_list = build_exercise_index(workouts)
    
    # Build summary
    total_reps = sum(w['volume_reps'] or 0 for w in workouts)
    total_volume = sum(w['volume_lb'] or 0 for w in workouts)
    weighted_exercises = [w for w in workouts if w['exercises']]
    
    # Build per-exercise progression data
    exercise_progress = {}
    for name, entries in sorted(by_exercise.items()):
        progression = []
        for e in entries:
            progression.append({
                'date': e['date'],
                'sets': e['sets'],
                'weight_lb': e['best_weight_lb'],
                'reps': e['best_reps'],
                'bodyweight': e['bodyweight'],
            })
        exercise_progress[name] = progression
    
    ladder_data = {
        'workouts': len(workouts),
        'total_reps': total_reps,
        'total_volume_lb': round(total_volume, 1),
        'date_range': {
            'start': workouts[0]['date'] if workouts else None,
            'end': workouts[-1]['date'] if workouts else None,
        },
        'exercises': exercise_list,
        'exercise_count': len(exercise_list),
        'by_exercise': exercise_progress,
        'workout_details': [{
            'date': w['date'],
            'title': w['title'],
            'volume_reps': w['volume_reps'],
            'volume_lb': w['volume_lb'],
            'exercise_count': len(w['exercises']),
            'exercises': [{
                'name': e['name'],
                'sets': e['sets'],
                'weight_lb': e['best_weight_lb'],
                'reps': e['best_reps'],
                'bodyweight': e['bodyweight'],
            } for e in w['exercises'] if e['name']],
        } for w in workouts if w['exercises']],
    }
    
    # Read existing dashboard JSON
    dashboard = json.loads(DASHBOARD_JSON.read_text(encoding='utf-8'))
    dashboard['ladder_exercises'] = ladder_data
    
    # Write back
    DASHBOARD_JSON.write_text(json.dumps(dashboard, indent=2), encoding='utf-8')
    
    print(f"✅ Added ladder_exercises to {DASHBOARD_JSON}")
    print(f"   {len(workouts)} workouts parsed")
    print(f"   {len(exercise_list)} unique exercises")
    print(f"   {total_reps} total reps, {round(total_volume, 1):,} lb total volume")
    print(f"   Date range: {ladder_data['date_range']['start']} → {ladder_data['date_range']['end']}")
    print()
    # Show exercises with most entries
    print("Most-tracked exercises:")
    for name, entries in sorted(by_exercise.items(), key=lambda x: -len(x[1]))[:10]:
        weights = [e['best_weight_lb'] for e in entries if e['best_weight_lb']]
        w_str = f"weights {'→'.join(str(w) for w in sorted(set(weights)))} lb" if weights else "BW"
        print(f"  {name}: {len(entries)} sessions — {w_str}")


if __name__ == '__main__':
    main()