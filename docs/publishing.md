# Publishing Checklist

Use this checklist before pushing Activity Journal to GitHub.

## Include

These paths are intended for the repository:

- `README.md`
- `.gitignore`
- `requirements.txt`
- `config/activity-journal.example.json`
- `scripts/`
- `browser_extension/chatgpt-live-capture/`
- `prompts/`
- `docs/`

Only the unpacked extension source belongs in the repository. Packaged extension artifacts and signing keys should live under `%LOCALAPPDATA%\ActivityJournal\browser_extension\` or another local-only path configured with `ACTIVITY_JOURNAL_EXTENSION_ARTIFACT_DIR`.

## Exclude

These paths are local-only and must not be committed:

- `config/activity-journal.json`
- `config/*.bak`
- `journal/`
- `imports/`
- `inbox/`
- `manual_notes/`
- `browser_extension/*.pem`
- `browser_extension/*.crx`
- `browser_extension/*-update.xml`
- `*.sqlite`, `*.sqlite3`, `*.db`
- `__pycache__/`, `*.pyc`

## Recommended Commands

After creating a Git repository, inspect the candidate files before committing:

```powershell
git status --short --ignored
```

Run a targeted scan for sensitive paths and keywords:

```powershell
rg -n -i "(token|secret|password|private key|BEGIN .*PRIVATE KEY|\.pem|\.sqlite|journal/raw)" README.md docs scripts browser_extension config/activity-journal.example.json prompts .gitignore requirements.txt
```

Expected matches should be documentation or code references to environment variable names such as `NOTION_TOKEN`. Real token values, private keys, raw logs, and SQLite files should not be staged.
