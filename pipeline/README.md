# Health OS pipeline

Everything that collects, processes and publishes David's health data lives here. Hermes agents (Heath) only *read* the results and write commentary; no data logic lives in agent prompts or profile folders.

## Daily schedule (Heath's Hermes cron, Pacific time)

| Time | Job | Runs |
|---|---|---|
| 07:15 | Glooko pre-sync login | `glooko_sync_trigger.sh` (Glooko pulls Omnipod 5 data ~30 min after a login) |
| 08:00 | Morning Brief | `refresh.sh`, then Heath writes the brief and calls `write_heath_note.py` |
| 08:25 | Health Portal publish | `publish_portal.sh` (picks up Heath's note) |
| Sat 08:10 | Weekly Health OS report | `weekly_health_report.py` |
| Every 5 min | Live glucose watch | `glucose_watch.py`: pulls Dexcom Share and wakes Heath only when a rule fires (low, heading low, fast rise, >250 for 2 h) |

Heath's profile (`/root/.hermes/profiles/heath/scripts/`) holds thin wrappers with the same names that call these files, because Hermes only runs scripts from that folder.

## Scripts

| File | What it does |
|---|---|
| `refresh.sh` | The 08:00 chain: Glooko export → WHOOP sync → DEXA sync → `../generate_dashboard.py` → Ladder log archive → portal publish |
| `glooko_export_refresh.py` | Logs in to Glooko and downloads the last 30 days of CGM/pump CSVs to `/root/health-data/glooko/raw/` |
| `glooko_sync_trigger.sh` | Silent 07:15 login so the 08:00 export is fresh |
| `whoop_history_sync.py` | Keeps all WHOOP recovery/sleep/workout records in `/root/health-data/whoop/whoop_history.sqlite` (`--backfill` refetches everything) |
| `dexa_sync.py` | Downloads new BodySpec PDFs from Google Drive, parses them into `/root/health-data/dexa/dexa_measurements.json` |
| `ladder_log_archive.py` | Moves Ladder Workout Log entries older than 30 days into monthly archive notes |
| `build_portal_data.py` | Builds `portal_data.json` from the sqlite, DEXA, Ladder export, food log, labs and Heath's note |
| `publish_portal.sh` | Builds the portal page + data into `../docs/` and pushes if anything changed |
| `portal/` | Portal source: `template.html` (layout/CSS), `app.js` (views and charts), `build_index.py` (assembles `docs/index.html`) |
| `write_heath_note.py` | Heath saves its short morning note for the portal |
| `live_glucose.py` | Shared code for near-real-time glucose from Dexcom Share (via `pydexcom` in `/root/health-pipeline-home/pylib`), stored in `/root/health-data/dexcom/live.sqlite` |
| `glucose_watch.py` | The 5-minute watch: fetch, apply rules (thresholds, cooldowns, quiet hours 10:30 PM–6:30 AM, daily cap), print a trigger for Heath or `{"wakeAgent": false}`; refreshes today's WHOOP workouts every 30 min |
| `glucose_now.py` | Heath's on-demand "glucose right now" check |
| `sutter_avs_parse.py` | Parses Sutter My Health Online after-visit summaries (HTML, or a zip of them in `/root/health-data/sutter/avs/`) into `visits.json` for the portal Labs view |
| `settings_analysis.py` | How well the pump settings fit by time of day and training load (meal and correction outcomes, daily automated basal). Heath runs it for settings tuning |
| `set_dexcom_login.sh` | Run by David to save his Dexcom login to Heath's `.env` (password typed hidden) |
| `weekly_health_report.py`, `overnight_hypo_check.py` | Older Heath report scripts |

## Data and secrets (never committed)

- Raw and derived data: `/root/health-data/` (glooko, whoop, dexa, ladder, sutter, pharmacy, supplies).
- Runtime state: `/root/health-portal/` (Heath's note, backups).
- Logins: `/root/health-pipeline-home/` is the pipeline's own HOME. It holds the GitHub CLI login used to push and a copy of the Google login used for Drive (Scout's copy is the fallback). Glooko credentials, `GLOOKO_CODE` and the Dexcom login (`DEXCOM_USERNAME`/`DEXCOM_PASSWORD`) stay in Heath's `.env`; WHOOP uses Heath's `whoop-pp-cli` login.
- This repo is public. Keep identifiers, tokens and raw exports out of it.

## Manual inputs

- **Ladder:** request a full export from the Ladder app and replace the two CSVs in `/root/health-data/ladder/`. Newer sessions in between come from Heath's screen-recording ingest.
- **DEXA:** drop BodySpec PDFs in the shared DEXA folder on Google Drive; the next 08:00 run picks them up.
- **Food:** text or photograph meals to Heath; they land in Obsidian `03 Health/Training & Diet/Daily Food Log.md`, which `build_portal_data.py` reads.
