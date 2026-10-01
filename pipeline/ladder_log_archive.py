#!/usr/bin/env python3
"""Sweep old Ladder Workout Log entries into monthly archive files.

Mirrors the podcast-digest archive sweep (podcast_digest_collect.py, 2026-09-08):
a rolling 30-day window in the live note, older entries moved into
Ladder Workout Log - Archive/YYYY-MM.md. The live file was already 1,333
lines / 27 entries after 5 weeks with no archive mechanism -- same shape
as the podcast-digest bug before it was fixed, just not yet hit. Added
2026-09-09 as part of the Health folder audit.

Idempotent: entries already present in the target monthly archive file
(matched by exact heading line) are not re-appended.
"""
import re
from datetime import datetime, timedelta
from pathlib import Path

OBSIDIAN_HEALTH_DIR = Path('/root/obsidian/David OS/03 Health/Training & Diet')
LOG_PATH = OBSIDIAN_HEALTH_DIR / 'Ladder Workout Log.md'
ARCHIVE_DIR = OBSIDIAN_HEALTH_DIR / 'Ladder Workout Log - Archive'
ARCHIVE_WINDOW_DAYS = 30

HEADING_RE = re.compile(r'^## (\d{4}-\d{2}-\d{2})\b.*$', re.MULTILINE)


def archive_fm(month_label: str) -> str:
    today = datetime.now().strftime('%Y-%m-%d')
    return (
        '---\n'
        'tags:\n'
        '  - health/fitness\n'
        f'created: {today}\n'
        'status: archive\n'
        'owner: Heath\n'
        '---\n\n'
        f'# Ladder Workout Log — Archive — {month_label}\n\n'
        f'Archived from [[Ladder Workout Log]] automatically by the daily health dashboard refresh, per the {ARCHIVE_WINDOW_DAYS}-day rolling-window rule.\n\n'
    )


def main() -> None:
    if not LOG_PATH.exists():
        print(f'WARNING: {LOG_PATH} not found; nothing to archive.')
        return

    text = LOG_PATH.read_text(encoding='utf-8')
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        print('No dated entries found; nothing to archive.')
        return

    header = text[:matches[0].start()]
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append((m.group(1), text[m.start():end]))

    cutoff = datetime.now() - timedelta(days=ARCHIVE_WINDOW_DAYS)
    keep = []
    to_archive = {}  # month_label -> list of entry text
    for date_str, body in entries:
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            keep.append(body)
            continue
        if d < cutoff:
            month_label = d.strftime('%Y-%m')
            to_archive.setdefault(month_label, []).append(body)
        else:
            keep.append(body)

    if not to_archive:
        print('No entries older than the window; nothing to archive.')
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived_count = 0
    for month_label, bodies in to_archive.items():
        archive_path = ARCHIVE_DIR / f'{month_label}.md'
        if archive_path.exists():
            existing = archive_path.read_text(encoding='utf-8')
        else:
            existing = archive_fm(month_label)

        for body in bodies:
            heading_line = body.splitlines()[0]
            if heading_line in existing:
                continue  # already archived, skip duplicate
            existing = existing.rstrip('\n') + '\n\n' + body.rstrip('\n') + '\n'
            archived_count += 1

        archive_path.write_text(existing, encoding='utf-8')

    new_live = header + ''.join(keep)
    new_live = re.sub(
        r'^updated: \d{4}-\d{2}-\d{2}$',
        f'updated: {datetime.now().strftime("%Y-%m-%d")}',
        new_live,
        count=1,
        flags=re.MULTILINE,
    )
    LOG_PATH.write_text(new_live, encoding='utf-8')
    print(f'Archived {archived_count} entr{"y" if archived_count == 1 else "ies"} across {len(to_archive)} month(s).')


if __name__ == '__main__':
    main()
