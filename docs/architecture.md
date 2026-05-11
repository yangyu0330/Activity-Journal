# Architecture

Activity Journal is a local-first evidence pipeline.

## Flow

1. Collect evidence from Codex history, Git repositories, external inboxes, browser metadata, recent files, and foreground app activity.
2. Write raw date-scoped evidence under `journal/raw/`.
3. Render reviewable Markdown under `journal/daily/`, `journal/weekly/`, `journal/questions/`, and `journal/projects/`.
4. Index normalized events into SQLite for local search.
5. Optionally sync summaries and delayed final logs to Notion.
6. Expose local status through the health checker, tray app, settings UI, and dashboard.

## Main Scripts

- `scripts/run_daily.ps1`: daily orchestration entrypoint.
- `scripts/collect_daily.py`: evidence collection and daily Markdown rendering.
- `scripts/project_review.py`: project-level rollups.
- `scripts/question_quality.py`: diagnostics for missing context.
- `scripts/sync_sqlite.py`: SQLite event indexing.
- `scripts/notion_sync.py`: Notion database/page sync.
- `scripts/project_health.py`: system health report.
- `scripts/dashboard_app.py`: local browser dashboard.
- `scripts/settings_app.py`: Tk settings UI.
- `scripts/tray_app.py`: Windows tray controls.

## Data Boundaries

Repository files are code, docs, prompts, public examples, and browser-extension source. Personal data stays local:

- `journal/`
- `imports/`
- `inbox/`
- `manual_notes/`
- `config/activity-journal.json`
- local SQLite databases
- extension signing artifacts

The `.gitignore` and publishing checklist enforce this boundary.
