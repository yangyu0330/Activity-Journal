# Privacy and Security

Activity Journal is local-first. It writes journals, raw captures, and indexes to local files under this project unless you explicitly enable Notion sync.

## Never Commit

Do not commit these paths to a public repository:

- `journal/`
- `imports/`
- `inbox/`
- `manual_notes/`
- `config/activity-journal.json`
- `config/*.bak`
- `browser_extension/*.pem`
- `browser_extension/*.crx`
- `browser_extension/*-update.xml`
- `*.sqlite`, `*.sqlite3`, `*.db`

The repository `.gitignore` excludes these paths. Keep it in place before creating commits.

## Local Data

Depending on your configuration, the system may store:

- Codex history/session metadata, and optionally text excerpts
- Git repository activity and recently modified file paths
- Manual notes from `manual_notes/`
- ChatGPT export files placed under `imports/chatgpt/`
- ChatGPT/Gemini live page text sent by the optional browser extension
- Chromium browser history metadata such as page titles, URLs, visit counts, and timestamps
- Foreground app/window titles, process names, timestamps, and optional accessibility text
- SQLite search/index data under `journal/activity_journal.sqlite3`

The example config defaults `external_inputs.enabled` and `external_inputs.include_raw_text` to `false`. Enable richer capture only after reviewing the storage impact.

## Secrets

Notion credentials are not stored in config. Use `scripts/set_notion_token.ps1` to save `NOTION_TOKEN` as a Windows user environment variable, or set `NOTION_TOKEN` in your shell before running sync.

Chrome extension private keys (`.pem`) and packaged CRX/update files are local signing artifacts. Do not publish them. Keep them in `%LOCALAPPDATA%\ActivityJournal\browser_extension\` or set `ACTIVITY_JOURNAL_EXTENSION_ARTIFACT_DIR` to another local-only directory. Load `browser_extension/chatgpt-live-capture/` as an unpacked extension when possible.

## Pre-Publish Check

Before publishing, run a status check and a secret scan:

```powershell
python scripts/settings_app.py --validate-config
python scripts/project_health.py --date 2026-05-09 --no-write
rg -n -i "(token|secret|password|private key|BEGIN .*PRIVATE KEY|\\.pem|\\.sqlite|journal/raw)" README.md docs scripts browser_extension config/activity-journal.example.json prompts .gitignore .gitattributes .github CONTRIBUTING.md LICENSE requirements.txt
```

Review matches manually. Code references to environment variable names such as `NOTION_TOKEN` are expected; real token values and private keys are not.
