# Contributing

Thanks for improving Activity Journal.

## Local Setup

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_local.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File scripts/install_local.ps1
python scripts/settings_app.py --validate-config
python scripts/smoke_tests.py
```

## Privacy Rules

Do not commit local activity data or credentials. These paths are intentionally ignored:

- `journal/`
- `imports/`
- `inbox/`
- `manual_notes/`
- `config/activity-journal.json`
- SQLite database files
- browser extension signing artifacts

Before opening a pull request, run the publishing scan in `docs/publishing.md`.

## Development Notes

- Keep new features local-first by default.
- Prefer standard-library implementations unless a dependency materially improves the user workflow.
- Add focused smoke tests for new scripts, parsers, and workflow behavior.
- Keep generated data out of tests unless it is synthetic and committed under `docs/`.
